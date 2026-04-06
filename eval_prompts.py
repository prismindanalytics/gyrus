#!/usr/bin/env python3
"""Gyrus Prompt Quality Evaluation Framework.

Scores extraction and merge quality against hand-curated golden fixtures.
Supports iterative prompt tuning: curate → eval → tweak → re-eval → compare.

Usage:
    python3 ingest.py --eval                    # Run full eval
    python3 ingest.py --eval --eval-type extraction  # Extraction only
    python3 ingest.py --eval --eval-deep        # Include LLM hallucination checks
    python3 ingest.py --eval-curate             # Create fixtures interactively
    python3 ingest.py --eval-save-prompt v1     # Save current prompts
    python3 ingest.py --eval --eval-compare v1 v2  # Compare prompt versions
    python3 ingest.py --eval --eval-regression  # Exit 1 if quality dropped
"""

import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path


# ─── Fixture Loading ───


def _eval_dir(base_dir):
    d = Path(base_dir) / "eval"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _fixtures_dir(base_dir, eval_type):
    d = _eval_dir(base_dir) / "fixtures" / eval_type
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_fixtures(base_dir, eval_type="extraction"):
    """Load all fixtures of the given type."""
    fixtures = []
    d = _fixtures_dir(base_dir, eval_type)
    for f in sorted(d.glob("*.json")):
        try:
            fixtures.append(json.loads(f.read_text()))
        except (json.JSONDecodeError, IOError) as e:
            print(f"  Warning: skipping {f.name}: {e}")
    return fixtures


# ─── Thought Matching ───


def _keyword_overlap(text1, text2, min_overlap=0.5):
    """Check if two texts share enough significant keywords."""
    import re
    stop = {"the", "a", "an", "is", "was", "are", "were", "be", "been", "being",
            "have", "has", "had", "do", "does", "did", "will", "would", "could",
            "should", "may", "might", "shall", "can", "to", "of", "in", "for",
            "on", "with", "at", "by", "from", "as", "into", "through", "and",
            "but", "or", "nor", "not", "so", "yet", "both", "that", "this",
            "it", "its", "they", "them", "their", "we", "our", "you", "your"}
    def keywords(text):
        words = set(re.findall(r'\b[a-z]{3,}\b', text.lower())) - stop
        return words
    k1, k2 = keywords(text1), keywords(text2)
    if not k1 or not k2:
        return 0.0
    overlap = len(k1 & k2)
    return overlap / min(len(k1), len(k2))


def match_thoughts(extracted, golden, threshold=0.5):
    """Match extracted thoughts to golden thoughts.

    Uses SequenceMatcher first, falls back to keyword overlap for
    semantically similar but lexically different thoughts.
    Returns list of (extracted_idx, golden_idx, score) tuples.
    """
    matches = []
    used_golden = set()

    # Score all pairs with both methods
    all_pairs = []
    for ei, et in enumerate(extracted):
        if not isinstance(et, dict):
            continue
        for gi, gt in enumerate(golden):
            ec = et.get("content", "").lower()
            gc = gt.get("content", "").lower()
            seq_score = SequenceMatcher(None, ec, gc).ratio()
            kw_score = _keyword_overlap(ec, gc)
            # Use the higher of the two signals
            score = max(seq_score, kw_score * 0.9)  # slight discount for keyword-only
            all_pairs.append((score, ei, gi))

    all_pairs.sort(reverse=True)
    used_extracted = set()

    for score, ei, gi in all_pairs:
        if ei in used_extracted or gi in used_golden:
            continue
        if score >= threshold:
            matches.append((ei, gi, score))
            used_extracted.add(ei)
            used_golden.add(gi)

    return matches


# ─── Extraction Scoring ───


def score_extraction(extracted, fixture):
    """Score extracted thoughts against a golden fixture.

    Returns dict with individual metrics and composite score.
    """
    golden = fixture.get("golden_thoughts", [])
    negative = fixture.get("negative_assertions", [])
    count_range = fixture.get("expected_thought_count_range", [0, 999])

    # Filter to dict thoughts only
    extracted = [t for t in extracted if isinstance(t, dict)]

    # Match
    matches = match_thoughts(extracted, golden)
    matched_golden_idx = {gi for _, gi, _ in matches}
    matched_extracted_idx = {ei for ei, _, _ in matches}

    # Recall: fraction of required golden thoughts captured
    required = [i for i, g in enumerate(golden) if g.get("required", True)]
    if required:
        recall = len([i for i in required if i in matched_golden_idx]) / len(required)
    else:
        recall = 1.0

    # Precision: fraction of extracted thoughts that match any golden thought
    precision = len(matched_extracted_idx) / len(extracted) if extracted else 1.0

    # Noise leakage: check negative assertions
    noise_hits = 0
    noise_details = []
    for t in extracted:
        content = t.get("content", "")
        for neg in negative:
            pattern = neg.get("pattern", "")
            if pattern and re.search(pattern, content, re.IGNORECASE):
                noise_hits += 1
                noise_details.append({
                    "thought": content[:80],
                    "pattern": pattern,
                    "reason": neg.get("reason", "")
                })
                break
    noise_clean = 1.0 - (noise_hits / len(extracted)) if extracted else 1.0

    # Project attribution accuracy
    correct_attrs = 0
    attr_total = 0
    for ei, gi, _ in matches:
        et = extracted[ei]
        gt = golden[gi]
        attr_total += 1
        # Normalize project names: "Meal Planner" == "meal-planner" == "meal_planner"
        et_proj = (et.get("project") or "").lower().replace(" ", "-").replace("_", "-")
        gt_proj = (gt.get("project") or "").lower().replace(" ", "-").replace("_", "-")
        et_kind = et.get("kind") or ""
        gt_kind = gt.get("kind") or ""
        # For ideas, project name doesn't matter
        proj_match = (et_proj == gt_proj) or (gt_kind == "idea")
        # Also accept fuzzy project match (SequenceMatcher > 0.7)
        if not proj_match and et_proj and gt_proj:
            proj_match = SequenceMatcher(None, et_proj, gt_proj).ratio() > 0.7
        kind_match = (et_kind == gt_kind)
        if proj_match and kind_match:
            correct_attrs += 1
    project_accuracy = correct_attrs / attr_total if attr_total else 1.0

    # Count calibration
    count_ok = count_range[0] <= len(extracted) <= count_range[1]
    count_calibration = 1.0 if count_ok else 0.0

    # Composite
    composite = (
        0.30 * recall +
        0.30 * precision +
        0.20 * noise_clean +
        0.10 * project_accuracy +
        0.10 * count_calibration
    )

    # Details for debugging
    missed_golden = [
        golden[i]["content"][:80]
        for i in range(len(golden))
        if golden[i].get("required", True) and i not in matched_golden_idx
    ]

    return {
        "recall": round(recall, 3),
        "precision": round(precision, 3),
        "noise_clean": round(noise_clean, 3),
        "project_accuracy": round(project_accuracy, 3),
        "count_calibration": count_calibration,
        "composite": round(composite, 3),
        "extracted_count": len(extracted),
        "golden_count": len(golden),
        "matched": len(matches),
        "missed_golden": missed_golden,
        "noise_details": noise_details,
    }


# ─── Merge Scoring ───


def _parse_sections(markdown):
    """Parse markdown into {section_name: content} dict."""
    sections = {}
    current = None
    lines = []
    for line in markdown.split("\n"):
        if line.startswith("## "):
            if current:
                sections[current] = "\n".join(lines)
            current = line[3:].strip()
            lines = []
        else:
            lines.append(line)
    if current:
        sections[current] = "\n".join(lines)
    return sections


def _check_structure(output, checks):
    """Run named structural validators. Returns (passed, total, details)."""
    validators = {
        "page_starts_with_h1": lambda o: o.strip().startswith("# "),
        "has_all_required_sections": lambda o: all(
            f"## {s}" in o for s in [
                "Status", "Overview", "Architecture", "Key Decisions",
                "Open Questions", "Timeline"
            ]
        ),
        "key_decisions_is_chronological": lambda o: _dates_chronological(o, "Key Decisions"),
        "has_change_summary": lambda o: "CHANGE_SUMMARY:" in o,
    }

    passed = 0
    details = []
    for check_name in checks:
        fn = validators.get(check_name)
        if fn:
            ok = fn(output)
            if ok:
                passed += 1
            details.append({"check": check_name, "passed": ok})
        else:
            details.append({"check": check_name, "passed": False, "error": "unknown validator"})

    return passed, len(checks), details


def _dates_chronological(markdown, section_name):
    """Check if dates in a section are in ascending order."""
    sections = _parse_sections(markdown)
    content = sections.get(section_name, "")
    dates = re.findall(r"\d{4}-\d{2}-\d{2}", content)
    return dates == sorted(dates)


def _detect_hallucinated_entities(output, input_texts):
    """Find named entities in output not grounded in inputs."""
    # Extract capitalized multi-word terms, backticked terms, URLs
    entity_pattern = re.compile(r'(?:`([^`]+)`|(?<!\w)([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)(?!\w))')
    output_entities = set()
    for m in entity_pattern.finditer(output):
        entity = m.group(1) or m.group(2)
        if entity and len(entity) > 3:
            output_entities.add(entity.lower())

    # Check which are grounded
    combined_input = " ".join(input_texts).lower()
    ungrounded = [e for e in output_entities if e not in combined_input]
    return ungrounded


def _detect_date_fabrication(output, input_texts):
    """Find dates in output not present in any input."""
    output_dates = set(re.findall(r"\d{4}-\d{2}-\d{2}", output))
    input_dates = set()
    for t in input_texts:
        input_dates.update(re.findall(r"\d{4}-\d{2}-\d{2}", t))
    fabricated = output_dates - input_dates
    return list(fabricated)


def _detect_quantifier_inflation(output, input_text):
    """Detect weak→strong qualifier escalations."""
    escalations = [
        (r"\bexploring\b", r"\bcommitted to\b"),
        (r"\bmight\b", r"\bwill\b"),
        (r"\bconsidering\b", r"\bdecided\b"),
        (r"\bone session\b", r"\bconsistently\b"),
        (r"\binitial\b", r"\bmature\b"),
        (r"\bprototype\b", r"\bproduction\b"),
    ]
    flags = []
    input_lower = input_text.lower()
    output_lower = output.lower()
    for weak, strong in escalations:
        if re.search(weak, input_lower) and re.search(strong, output_lower):
            if not re.search(strong, input_lower):
                flags.append({"weak": weak, "strong": strong})
    return flags


def score_merge(output, fixture):
    """Score merge output against a golden fixture.

    Returns dict with individual metrics and composite score.
    """
    required = fixture.get("required_content", [])
    forbidden = fixture.get("forbidden_content", [])
    struct_checks = fixture.get("structural_checks", [])
    existing_page = fixture.get("existing_page", "")
    input_thoughts = fixture.get("input_thoughts", [])

    # Content completeness
    satisfied = 0
    completeness_details = []
    for req in required:
        section = req.get("section", "")
        sections = _parse_sections(output)
        section_text = sections.get(section, output)  # fallback to full page

        must_have = req.get("must_contain", [])
        must_not = req.get("must_not_contain", [])

        have_ok = all(s.lower() in section_text.lower() for s in must_have)
        not_ok = all(s.lower() not in section_text.lower() for s in must_not)

        if have_ok and not_ok:
            satisfied += 1
        completeness_details.append({
            "section": section,
            "must_contain_ok": have_ok,
            "must_not_contain_ok": not_ok,
        })

    completeness = satisfied / len(required) if required else 1.0

    # Hallucination check
    forbidden_hits = 0
    hallucination_details = []
    for fb in forbidden:
        pattern = fb.get("pattern", "")
        if pattern and re.search(pattern, output, re.IGNORECASE):
            forbidden_hits += 1
            hallucination_details.append({
                "pattern": pattern,
                "reason": fb.get("reason", ""),
            })
    hallucination_clean = 1.0 - (forbidden_hits / len(forbidden)) if forbidden else 1.0

    # Structural integrity
    if struct_checks:
        s_passed, s_total, s_details = _check_structure(output, struct_checks)
        structural = s_passed / s_total
    else:
        structural = 1.0
        s_details = []

    # Staleness detection
    stale_checks = fixture.get("staleness_checks", [])
    stale_verified = 0
    for sc in stale_checks:
        claim = sc.get("claim_pattern", "")
        if claim and re.search(claim, output, re.IGNORECASE):
            # Check if evidence supports the claim
            rule = sc.get("evidence_required", "")
            if "thought_newer_than" in rule:
                days = int(re.search(r"(\d+)", rule).group(1))
                today = datetime.now().date()
                has_recent = any(
                    t.get("created_at", "")[:10] and
                    (today - datetime.fromisoformat(t["created_at"][:10]).date()).days <= days
                    for t in input_thoughts
                    if t.get("created_at")
                )
                if has_recent:
                    stale_verified += 1
    staleness = stale_verified / len(stale_checks) if stale_checks else 1.0

    # Append-only compliance
    if existing_page:
        old_sections = _parse_sections(existing_page)
        new_sections = _parse_sections(output)
        preserved = 0
        total_entries = 0
        for section_name in ["Key Decisions", "Timeline & History"]:
            old_entries = re.findall(r"- \[.+?\].*", old_sections.get(section_name, ""))
            total_entries += len(old_entries)
            new_text = new_sections.get(section_name, "")
            for entry in old_entries:
                # Check if the core content is preserved (fuzzy)
                entry_key = entry[:50].lower()
                if entry_key in new_text.lower():
                    preserved += 1
        append_only = preserved / total_entries if total_entries else 1.0
    else:
        append_only = 1.0  # N/A for initial merge

    # Additional hallucination detectors
    input_texts = [t.get("content", "") for t in input_thoughts] + [existing_page]
    ungrounded = _detect_hallucinated_entities(output, input_texts)
    fabricated_dates = _detect_date_fabrication(output, input_texts)
    input_combined = " ".join(t.get("content", "") for t in input_thoughts)
    inflation = _detect_quantifier_inflation(output, input_combined)

    # Composite
    composite = (
        0.30 * hallucination_clean +
        0.25 * completeness +
        0.20 * structural +
        0.15 * staleness +
        0.10 * append_only
    )

    return {
        "completeness": round(completeness, 3),
        "hallucination_clean": round(hallucination_clean, 3),
        "structural": round(structural, 3),
        "staleness": round(staleness, 3),
        "append_only": round(append_only, 3),
        "composite": round(composite, 3),
        "completeness_details": completeness_details,
        "hallucination_details": hallucination_details,
        "structural_details": s_details,
        "ungrounded_entities": ungrounded[:10],
        "fabricated_dates": fabricated_dates,
        "quantifier_inflation": inflation,
    }


# ─── Prompt Versioning ───


def save_prompt_version(base_dir, name, extraction_prompt, merge_prompt):
    """Save current prompts as a named version."""
    d = _eval_dir(base_dir) / "prompts"
    d.mkdir(parents=True, exist_ok=True)
    data = {
        "name": name,
        "saved_at": datetime.now().isoformat(),
        "extraction_prompt": extraction_prompt,
        "merge_prompt": merge_prompt,
    }
    (d / f"{name}.json").write_text(json.dumps(data, indent=2) + "\n")
    print(f"  ✓ Saved prompt version: {name}")


def load_prompt_version(base_dir, name):
    """Load a saved prompt version."""
    path = _eval_dir(base_dir) / "prompts" / f"{name}.json"
    if not path.exists():
        print(f"  Error: prompt version '{name}' not found")
        return None
    return json.loads(path.read_text())


# ─── Results ───


def save_results(base_dir, results):
    """Save eval results to timestamped JSON."""
    d = _eval_dir(base_dir) / "results"
    d.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    path = d / f"{ts}.json"
    path.write_text(json.dumps(results, indent=2, default=str) + "\n")
    return path


def load_latest_results(base_dir):
    """Load the most recent eval results."""
    d = _eval_dir(base_dir) / "results"
    if not d.exists():
        return None
    files = sorted(d.glob("*.json"))
    if not files:
        return None
    return json.loads(files[-1].read_text())


# ─── Run Eval ───


def run_eval(args, base_dir, config):
    """Run prompt quality eval against golden fixtures."""
    import ingest

    eval_type = getattr(args, "eval_type", "both") or "both"
    deep = getattr(args, "eval_deep", False)
    regression = getattr(args, "eval_regression", False)
    compare = getattr(args, "eval_compare", None)

    # Ensure ingest._config is set (may have been reset by import)
    if not ingest._config.get("keys"):
        env_keys = {}
        env_file = Path(base_dir) / ".env"
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    env_keys[k.strip()] = v.strip().strip("\"'")
        ingest._config["keys"] = {
            k: v for k, v in {
                "anthropic": env_keys.get("ANTHROPIC_API_KEY", ""),
                "openai": env_keys.get("OPENAI_API_KEY", ""),
                "google": env_keys.get("GEMINI_API_KEY", "") or env_keys.get("GOOGLE_API_KEY", ""),
            }.items() if v
        }
    if config.get("extract_model"):
        ingest._config["extract_model"] = config["extract_model"]
    if config.get("merge_model"):
        ingest._config["merge_model"] = config["merge_model"]

    # Load previous results for delta display
    prev = load_latest_results(base_dir)

    results = {
        "timestamp": datetime.now().isoformat(),
        "extract_model": config.get("extract_model", "gpt-4.1-mini"),
        "merge_model": config.get("merge_model", "sonnet"),
        "extraction": {},
        "merge": {},
    }

    t0 = time.time()

    # ── Extraction eval ──
    if eval_type in ("extraction", "both"):
        fixtures = load_fixtures(base_dir, "extraction")
        if not fixtures:
            print("  No extraction fixtures found in ~/.gyrus/eval/fixtures/extraction/")
            print("  Run --eval-curate to create some, or see docs for fixture format")
        else:
            print(f"\nEXTRACTION EVAL ({len(fixtures)} fixtures, model: {results['extract_model']})")
            print(f"{'Fixture':<30} {'Recall':>7} {'Precis':>7} {'Noise':>7} {'ProjAcc':>7} {'Count':>6} {'Score':>7}")

            scores = []
            for fixture in fixtures:
                fid = fixture.get("id", "unknown")
                text = fixture.get("session_text", "")
                workspace = fixture.get("workspace", "")

                # Run extraction
                extracted = ingest.call_claude(
                    text, None, workspace=workspace,
                    repo_groups=config.get("repo_groups")
                )

                score = score_extraction(extracted, fixture)
                scores.append(score)
                results["extraction"][fid] = score

                print(f"  {fid:<28} {score['recall']:>6.2f} {score['precision']:>6.2f} "
                      f"{score['noise_clean']:>6.2f} {score['project_accuracy']:>6.2f} "
                      f"{score['count_calibration']:>5.1f} {score['composite']:>6.2f}")

            # Average
            if scores:
                avg = {k: sum(s[k] for s in scores) / len(scores)
                       for k in ["recall", "precision", "noise_clean",
                                  "project_accuracy", "count_calibration", "composite"]}
                print(f"  {'AVERAGE':<28} {avg['recall']:>6.2f} {avg['precision']:>6.2f} "
                      f"{avg['noise_clean']:>6.2f} {avg['project_accuracy']:>6.2f} "
                      f"{avg['count_calibration']:>5.1f} {avg['composite']:>6.2f}")
                results["extraction"]["_average"] = {k: round(v, 3) for k, v in avg.items()}

                # Delta from previous
                if prev and prev.get("extraction", {}).get("_average"):
                    prev_avg = prev["extraction"]["_average"]["composite"]
                    delta = avg["composite"] - prev_avg
                    sign = "+" if delta >= 0 else ""
                    print(f"  vs. previous: {prev_avg:.2f} → {avg['composite']:.2f} ({sign}{delta:.2f})")

    # ── Merge eval ──
    if eval_type in ("merge", "both"):
        fixtures = load_fixtures(base_dir, "merge")
        if not fixtures:
            print("\n  No merge fixtures found in ~/.gyrus/eval/fixtures/merge/")
            print("  See docs for fixture format")
        else:
            print(f"\nMERGE EVAL ({len(fixtures)} fixtures, model: {results['merge_model']})")
            print(f"{'Fixture':<30} {'Compl':>6} {'Hallu':>6} {'Struct':>7} {'Stale':>6} {'Appnd':>6} {'Score':>7}")

            scores = []
            for fixture in fixtures:
                fid = fixture.get("id", "unknown")
                existing = fixture.get("existing_page", "")
                thoughts = fixture.get("input_thoughts", [])

                # Format thoughts for merge prompt
                thought_strs = []
                for t in thoughts:
                    src = t.get("source", "?")
                    dt = t.get("created_at", "?")[:10]
                    thought_strs.append(
                        f"- [{t.get('kind', 'project')}] [{dt}, {src}] {t.get('content', '')}"
                    )
                new_thoughts_text = "\n".join(thought_strs)

                prompt = ingest.MERGE_PROMPT.format(
                    page_content=existing,
                    new_thoughts=new_thoughts_text
                )

                try:
                    output = ingest.call_llm(prompt, role="merge", max_tokens=8192)
                except Exception as e:
                    print(f"  {fid:<28} ERROR: {e}")
                    continue

                score = score_merge(output, fixture)
                scores.append(score)
                results["merge"][fid] = score

                print(f"  {fid:<28} {score['completeness']:>5.2f} {score['hallucination_clean']:>5.2f} "
                      f"{score['structural']:>6.2f} {score['staleness']:>5.2f} "
                      f"{score['append_only']:>5.2f} {score['composite']:>6.2f}")

            if scores:
                avg = {k: sum(s[k] for s in scores) / len(scores)
                       for k in ["completeness", "hallucination_clean", "structural",
                                  "staleness", "append_only", "composite"]}
                print(f"  {'AVERAGE':<28} {avg['completeness']:>5.2f} {avg['hallucination_clean']:>5.2f} "
                      f"{avg['structural']:>6.2f} {avg['staleness']:>5.2f} "
                      f"{avg['append_only']:>5.2f} {avg['composite']:>6.2f}")
                results["merge"]["_average"] = {k: round(v, 3) for k, v in avg.items()}

                if prev and prev.get("merge", {}).get("_average"):
                    prev_avg = prev["merge"]["_average"]["composite"]
                    delta = avg["composite"] - prev_avg
                    sign = "+" if delta >= 0 else ""
                    print(f"  vs. previous: {prev_avg:.2f} → {avg['composite']:.2f} ({sign}{delta:.2f})")

    # Save results
    elapsed = time.time() - t0
    results["duration_seconds"] = round(elapsed, 1)
    path = save_results(base_dir, results)
    print(f"\nCost: ~${_estimate_cost(results, config):.2f} | Duration: {elapsed:.0f}s")
    print(f"Results: {path}")

    # Regression gate
    if regression and prev:
        failed = False
        for phase in ["extraction", "merge"]:
            curr_avg = results.get(phase, {}).get("_average", {}).get("composite", 0)
            prev_avg = prev.get(phase, {}).get("_average", {}).get("composite", 0)
            if prev_avg > 0 and curr_avg < prev_avg - 0.05:
                print(f"\n  REGRESSION: {phase} score dropped {prev_avg:.2f} → {curr_avg:.2f}")
                failed = True
        if failed:
            sys.exit(1)
        else:
            print("\n  ✓ No regressions detected")


def _estimate_cost(results, config):
    """Rough cost estimate for the eval run."""
    import ingest
    ext_count = len([k for k in results.get("extraction", {}) if not k.startswith("_")])
    merge_count = len([k for k in results.get("merge", {}) if not k.startswith("_")])
    ext_model = config.get("extract_model", "gpt-4.1-mini")
    merge_model = config.get("merge_model", "sonnet")
    ext_cost = ext_count * ingest._COST_PER_CALL.get(ext_model, 0.01)
    merge_cost = merge_count * ingest._COST_PER_CALL.get(merge_model, 0.03)
    return ext_cost + merge_cost


# ─── Curate Fixtures ───


def run_curate(args, base_dir):
    """Interactive fixture curation from real sessions."""
    import ingest

    print("\n  Gyrus Eval — Fixture Curation")
    print("  " + "=" * 40)

    session_id = getattr(args, "eval_session", None)

    store = ingest.MarkdownStorage(base_dir=str(base_dir))
    state = store.load_state()

    # Find sessions
    all_sessions = (
        ingest.find_claude_code_sessions(state) +
        ingest.find_cowork_sessions(state) +
        ingest.find_antigravity_sessions(state) +
        ingest.find_codex_sessions(state)
    )

    if session_id:
        all_sessions = [s for s in all_sessions if session_id in s["session_id"]]
        if not all_sessions:
            print(f"  Session '{session_id}' not found")
            return

    if not all_sessions:
        print("  No sessions found")
        return

    # Pick session
    if len(all_sessions) == 1:
        session = all_sessions[0]
    else:
        # Show options
        print(f"\n  Found {len(all_sessions)} sessions. Pick one:")
        for i, s in enumerate(all_sessions[:20]):
            ws = s.get("workspace", "")
            print(f"    [{i+1}] {s['type']}: {ws or s['session_id'][:30]}")
        try:
            choice = input(f"\n  Session [1]: ").strip()
            idx = int(choice) - 1 if choice else 0
            session = all_sessions[idx]
        except (ValueError, IndexError, EOFError):
            session = all_sessions[0]

    # Extract text
    EXTRACTORS = {
        "claude-code": lambda s: ingest.extract_claude_code_conversation(s["path"]),
        "cowork": lambda s: ingest.extract_cowork_conversation(s["path"], s.get("output_dir")),
        "antigravity": lambda s: ingest.extract_antigravity_session(s["path"]),
        "codex": lambda s: ingest.extract_codex_conversation(s["path"]),
    }
    fn = EXTRACTORS.get(session["type"])
    text = fn(session) if fn else ""

    if len(text) < 100:
        print("  Session too short for fixture creation")
        return

    print(f"\n  Session: {session['type']}: {session.get('workspace', session['session_id'][:30])}")
    print(f"  Text: {len(text)} chars")

    # Run current extraction
    print("\n  Running extraction with current prompt...")
    extracted = ingest.call_claude(
        text, None, workspace=session.get("workspace", ""),
        repo_groups={}
    )
    print(f"  Got {len(extracted)} thoughts")

    # Present for review
    golden = []
    for i, t in enumerate(extracted):
        if not isinstance(t, dict):
            continue
        content = t.get("content", "")
        project = t.get("project", "")
        kind = t.get("kind", "")
        print(f"\n  [{i+1}] [{kind}] [{project}] {content[:120]}")
        try:
            action = input("    Keep(enter) / Drop(d) / Edit(e): ").strip().lower()
        except EOFError:
            action = ""

        if action == "d":
            continue
        elif action == "e":
            try:
                new_content = input("    New content: ").strip()
            except EOFError:
                new_content = ""
            if new_content:
                t["content"] = new_content
            golden.append({"content": t["content"], "project": project,
                           "kind": kind, "tags": t.get("tags", []),
                           "required": True})
        else:
            golden.append({"content": content, "project": project,
                           "kind": kind, "tags": t.get("tags", []),
                           "required": True})

    # Ask for missed thoughts
    print("\n  Any thoughts that should have been extracted but weren't?")
    while True:
        try:
            extra = input("    Add thought (or Enter to finish): ").strip()
        except EOFError:
            break
        if not extra:
            break
        golden.append({"content": extra, "project": "", "kind": "project",
                       "tags": [], "required": True,
                       "notes": "manually added during curation"})

    # Build fixture
    fid = f"{len(list(_fixtures_dir(base_dir, 'extraction').glob('*.json'))) + 1:03d}-{session['type']}"
    fixture = {
        "id": fid,
        "description": f"Curated from {session['type']} session",
        "source_type": session["type"],
        "workspace": session.get("workspace", ""),
        "session_text": text,
        "created_from": f"Session {session['session_id']}",
        "golden_thoughts": golden,
        "negative_assertions": [
            {"pattern": r"git commit|npm install|pip install", "reason": "Tool command"},
            {"pattern": r"import \w+|from \w+ import", "reason": "Code import"},
        ],
        "expected_thought_count_range": [max(1, len(golden) - 2), len(golden) + 3],
    }

    # Save
    path = _fixtures_dir(base_dir, "extraction") / f"{fid}.json"
    path.write_text(json.dumps(fixture, indent=2, default=str) + "\n")
    print(f"\n  ✓ Saved fixture: {path}")
    print(f"  Golden thoughts: {len(golden)}")
    print(f"  Edit the fixture JSON to refine negative assertions and expected counts")
