# SPDX-License-Identifier: MIT
"""Versioned process scoring. Evidence is separate from the policy interpreting it."""
import ast
from collections import Counter
import hashlib
import json
import math

ALGORITHM = "retention-v2"
# Retention only. Resource use is recorded as fact in the proof vector and is
# deliberately not scored: judging efficiency requires a comparable result,
# which this recorder cannot observe.
WEIGHTS = {"human": .40, "artifact": .40, "task": .20}
# Frozen weights of the superseded algorithm. Kept so old proofs verify.
BALANCED_WEIGHTS = {"human": .28, "artifact": .28, "task": .14, "efficiency": .30}
LABELS = {"human": "Input retention", "artifact": "Artifact survival",
          "task": "Task fulfillment", "efficiency": "Resource discipline"}


def fingerprints(content, name):
    """At most 64 structural/content units; never retain source text."""
    kind = "content-blocks"
    try:
        text = content.decode("utf-8")
        if name.endswith(".py"):
            try:
                tree = ast.parse(text)
                def stable(node):
                    if isinstance(node, ast.AST):
                        return {"node": type(node).__name__, **{key: stable(value) for key, value in ast.iter_fields(node)
                                if value is not None and value != []}}
                    if isinstance(node, list):
                        return [stable(item) for item in node]
                    if isinstance(node, (bytes, complex)) or node is Ellipsis:
                        return repr(node)
                    return node
                units = [json.dumps(stable(node), sort_keys=True, ensure_ascii=True) for node in tree.body]
                kind = "python-ast"
            except (SyntaxError, ValueError, RecursionError):
                lines = [line.strip() for line in text.splitlines() if line.strip()]
                units = ["\n".join(lines[i:i + 8]) for i in range(0, len(lines), 8)]
        else:
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            units = ["\n".join(lines[i:i + 8]) for i in range(0, len(lines), 8)]
        hashes = [hashlib.sha256(unit.encode()).hexdigest() for unit in units[:64]]
    except (UnicodeError, RecursionError):
        kind = "binary-blocks"
        units = range(0, len(content), 4096)
        hashes = [hashlib.sha256(content[i:i + 4096]).hexdigest() for i in list(units)[:64]]
    return {"units": sorted(set(hashes)), "kind": kind, "truncated": len(units) > 64}


def collect_evidence(events, final_units, scope_units=0, scope_files=0, limited=False):
    """Retain final lineage, including deletion tombstones, against the actual tree.

    Input ownership is temporal unless an adapter supplies prompt_id. This is a
    retention proxy, not an assertion of semantic entailment or causality.
    """
    baseline, operations, lineage, prompts, tasks = {}, Counter(), {}, {}, {}
    current_prompt = None
    total_ops = 0
    gaps = 0
    for event in events:
        data, kind = event["data"], event["type"]
        if kind == "human.message":
            current_prompt = event["event_id"]
            prompts[current_prompt] = {"weight": max(1, data.get("tokens") or 1), "raw": 0, "retained": 0}
        elif kind == "coverage.gap":
            gaps += 1
        elif kind == "file.observed":
            path = data["path_hash"]
            if "units_before" not in data or "units_after" not in data:
                limited = True
                continue
            before, after = set(data["units_before"]), set(data["units_after"])
            baseline.setdefault(path, before)
            owner = data.get("prompt_id") or current_prompt
            for action, units in (("add", after - before), ("remove", before - after)):
                for unit in units:
                    total_ops += 1
                    operations[path] += 1
                    lineage[(path, action, unit)] = owner
                    if owner in prompts:
                        prompts[owner]["raw"] += 1
            limited = limited or data.get("units_truncated", False)
        elif kind == "task.change" and data.get("task_id") and data.get("status"):
            key = data["task_id"]
            old = tasks.get(key)
            attempts = (old["attempts"] if old else 1)
            if old and old["status"] in {"completed", "dropped"} and data["status"] == "active":
                attempts += 1
            tasks[key] = {"status": data["status"], "attempts": attempts,
                          "evidence": data.get("evidence", [])}
    retained = 0
    checked_ops = 0
    for path, before in baseline.items():
        if path not in final_units:
            limited = True
            continue
        after = set(final_units[path])
        checked_ops += operations[path]
        for action, units in (("add", after - before), ("remove", before - after)):
            for unit in units:
                key = (path, action, unit)
                if key not in lineage:
                    continue
                retained += 1
                owner = lineage[key]
                if owner in prompts:
                    prompts[owner]["retained"] += 1
    observed_prompts = [p for p in prompts.values() if p["raw"]]
    prompt_weight = sum(p["weight"] for p in observed_prompts)
    kept_weight = sum(p["weight"] * p["retained"] / p["raw"] for p in observed_prompts)
    completed = sum(1 for t in tasks.values() if t["status"] == "completed"
                    and t["evidence"] and all(path in final_units and final_units[path] for path in t["evidence"]))
    return {"artifact": {"retained": retained, "operations": total_ops, "checked_operations": checked_ops},
            "human": {"retained_weight": format(kept_weight, ".6f"), "observed_weight": prompt_weight,
                      "linked_prompts": len(observed_prompts), "prompts": len(prompts), "attribution": "temporal-proxy"},
            "task": {"completed": completed, "attempts": sum(t["attempts"] for t in tasks.values()),
                     "declared": len(tasks), "basis": "declared-with-committed-evidence"},
            "scope": {"units": max(scope_units, retained), "files": scope_files}, "limited": bool(limited), "gaps": gaps}


def grade(score):
    return next(label for threshold, label in ((90, "S"), (80, "A"), (65, "B"), (50, "C"), (35, "D"), (0, "E")) if score >= threshold)


def _score_balanced_v1(evidence, metrics):
    """Superseded algorithm, retained verbatim so sealed proofs keep verifying.

    Never edit this body: verification recomputes it and compares the result
    with the stored score. Behaviour changes belong in a new algorithm.
    """
    parts = {}
    def retain(key, numerator, denominator, reliability=1):
        ratio = numerator / denominator if denominator else None
        quality = (numerator + 2) / (denominator + 4) if denominator else .5
        confidence = (denominator / (denominator + 4) if denominator else 0) * reliability
        parts[key] = {"label": LABELS[key], "weight": str(BALANCED_WEIGHTS[key]),
                      "ratio": None if ratio is None else format(ratio, ".6f"),
                      "quality": format(quality, ".6f"), "confidence": format(confidence, ".6f")}
    art, human, task = (evidence[k] for k in ("artifact", "human", "task"))
    coverage = art["checked_operations"] / art["operations"] if art["operations"] else 0
    retain("artifact", art["retained"], art["operations"], coverage * (.8 if evidence["limited"] else 1))
    # Token mass weights prompts, but sample confidence depends on prompt count.
    hratio = float(human["retained_weight"]) / human["observed_weight"] if human["observed_weight"] else 0
    retain("human", hratio * human["linked_prompts"], human["linked_prompts"],
           .7 * (human["linked_prompts"] / human["prompts"] if human["prompts"] else 0))
    retain("task", task["completed"], task["attempts"])
    scope = max(1.0, math.sqrt(evidence["scope"]["units"]))
    resources = []
    for label, name, anchor, weight in (("Human input", "human", 800, .25), ("Visible output", "visible_ai", 1200, .15)):
        bucket = metrics[name]
        if bucket.get("events", bucket.get("messages", 0)) and bucket.get("tokens_complete", True):
            tokens = bucket.get("tokens_estimated", 0) + bucket.get("tokens_measured", 0)
            resources.append((label, tokens / (anchor * scope), weight))
    if metrics.get("reference_cost_complete_for_observed_calls"):
        resources.append(("Reference compute", float(metrics["reference_usd_known_subtotal"]) / (.5 * scope), .45))
    elif metrics.get("models") and all(m.get("input_tokens") is not None and m.get("output_tokens") is not None for m in metrics["models"].values()):
        tokens = sum(m["input_tokens"] + m["output_tokens"] + (m.get("cache_write_tokens") or 0) for m in metrics["models"].values())
        resources.append(("Model tokens (fallback)", tokens / (40000 * scope), .45))
    tool_calls = metrics.get("event_counts", {}).get("tool.call", 0)
    if tool_calls:
        resources.append(("Tool dispatches", tool_calls / (12 * scope), .15))
    available = sum(r[2] for r in resources)
    quality = sum(w / (1 + pressure ** .7) for _, pressure, w in resources) + .5 * (1 - available)
    confidence = available * min(1, evidence["scope"]["units"] / 8)
    parts["efficiency"] = {"label": LABELS["efficiency"], "weight": ".3", "ratio": format(quality, ".6f"),
                           "quality": format(quality, ".6f"), "confidence": format(confidence, ".6f"),
                           "resources": [{"name": name, "pressure": format(p, ".5f")} for name, p, _ in resources]}
    if evidence.get("gaps"):
        for part in parts.values():
            part["confidence"] = format(float(part["confidence"]) / (1 + evidence["gaps"]), ".6f")
    # Every component contributes around neutral; retain the missing-data penalty.
    latent = .5 + sum(BALANCED_WEIGHTS[k] * float(p["confidence"]) * (float(p["quality"]) - .5) for k, p in parts.items())
    value = 100 / (1 + math.exp(-5 * (latent - .5)))
    confidence = sum(BALANCED_WEIGHTS[k] * float(p["confidence"]) for k, p in parts.items())
    units = evidence["scope"]["units"]
    cohort = "small" if units < 8 else "medium" if units < 32 else "large" if units < 128 else "extensive"
    return {"algorithm": "balanced-v1", "value": format(value, ".1f"), "grade": grade(round(value, 1)),
            "confidence": format(confidence, ".4f"), "status": "provisional" if confidence < .65 else "established",
            "cohort": cohort, "components": parts, "evidence": evidence,
            "meaning": "process-retention-and-resource-score", "quality_verified": False}


def _score_retention_v2(evidence):
    """Retention-only 0-100 score: how much observed work survived into the commit.

    Resource use is not an input. A dimension with no evidence cedes its weight
    to the observed ones instead of pulling every score toward the neutral 50,
    and the score states which dimensions it was actually computed from.
    """
    parts = {}
    def retain(key, numerator, denominator, reliability=1):
        ratio = numerator / denominator if denominator else None
        # Beta(2,2) smoothing: a single observation cannot reach 0 or 1.
        quality = (numerator + 2) / (denominator + 4) if denominator else .5
        confidence = (denominator / (denominator + 4) if denominator else 0) * reliability
        parts[key] = {"label": LABELS[key], "weight": format(WEIGHTS[key], ".2f"),
                      "ratio": None if ratio is None else format(ratio, ".6f"),
                      "quality": format(quality, ".6f"), "confidence": format(confidence, ".6f")}
    art, human, task = (evidence[k] for k in ("artifact", "human", "task"))
    coverage = art["checked_operations"] / art["operations"] if art["operations"] else 0
    retain("artifact", art["retained"], art["operations"], coverage * (.8 if evidence["limited"] else 1))
    # Token mass weights prompts, but sample confidence depends on prompt count.
    hratio = float(human["retained_weight"]) / human["observed_weight"] if human["observed_weight"] else 0
    retain("human", hratio * human["linked_prompts"], human["linked_prompts"],
           .7 * (human["linked_prompts"] / human["prompts"] if human["prompts"] else 0))
    retain("task", task["completed"], task["attempts"])
    if evidence.get("gaps"):
        for part in parts.values():
            part["confidence"] = format(float(part["confidence"]) / (1 + evidence["gaps"]), ".6f")
    observed = {key: WEIGHTS[key] for key, part in parts.items() if float(part["confidence"]) > 0}
    total = sum(observed.values())
    latent = .5
    for key, part in parts.items():
        effective = observed.get(key, 0) / total if total else 0
        part["effective_weight"] = format(effective, ".6f")
        latent += effective * float(part["confidence"]) * (float(part["quality"]) - .5)
    value = 100 / (1 + math.exp(-5 * (latent - .5)))
    # Confidence follows the same basis as the score: the dimensions it used.
    confidence = sum(float(parts[key]["effective_weight"]) * float(parts[key]["confidence"]) for key in parts)
    units = evidence["scope"]["units"]
    cohort = "small" if units < 8 else "medium" if units < 32 else "large" if units < 128 else "extensive"
    return {"algorithm": "retention-v2", "value": format(value, ".1f"), "grade": grade(round(value, 1)),
            "confidence": format(confidence, ".4f"), "status": "provisional" if confidence < .65 else "established",
            "cohort": cohort, "components": parts, "evidence": evidence,
            "dimensions": {"scored": sorted(observed), "unscored": sorted(set(parts) - set(observed))},
            "meaning": "process-retention-score", "quality_verified": False}


def score(evidence, metrics=None, algorithm=ALGORITHM):
    """Dispatch on the recorded algorithm; a verifier never reinterprets old proofs."""
    if algorithm == "retention-v2":
        return _score_retention_v2(evidence)
    if algorithm == "balanced-v1":
        return _score_balanced_v1(evidence, metrics)
    raise ValueError("Unsupported score algorithm: " + str(algorithm))


def _add(total, value):
    """None means unknown and stays unknown; it is never treated as zero."""
    return None if total is None or value is None else total + value


ITERATION_ALGORITHM = "iteration-v1"
# Quantities that accumulate from T-1 to T. Unknown poisons the sum; it never becomes zero.
SUMMABLE = ("events", "prompts", "human_tokens", "visible_events", "visible_tokens",
            "model_calls", "input_tokens", "cached_input_tokens", "output_tokens",
            "reasoning_tokens", "tool_calls", "failed_tool_calls", "skill_uses",
            "sub_agents", "sessions", "runs", "coverage_gaps", "span_ms",
            "operations", "retained", "task_attempts", "task_completed",
            "files_changed", "priced_calls", "unpriced_calls", "actual_priced_calls")
MONEY = ("reference_usd", "actual_usd")


def iteration_contribution(proof):
    """What one sealed commit adds to the running history."""
    metrics = proof.get("summary") or {}
    evidence = ((proof.get("score") or {}).get("evidence")) or {}
    artifact, task, scope = (evidence.get(k) or {} for k in ("artifact", "task", "scope"))
    counts = metrics.get("event_counts") or {}
    window, activity = metrics.get("window") or {}, metrics.get("activity") or {}
    def text(bucket, field):
        data = metrics.get(bucket) or {}
        if data.get("tokens_complete") is False:
            return None
        return (data.get("tokens_measured") or 0) + (data.get("tokens_estimated") or 0)
    models = (metrics.get("models") or {}).values()
    def model_sum(field):
        total = 0
        for bucket in models:
            if bucket.get(field) is None:
                return None
            total += bucket[field]
        return total
    row = {"events": sum(counts.values()) if counts else 0,
           "prompts": (metrics.get("human") or {}).get("messages") or 0,
           "human_tokens": text("human", "tokens"),
           "visible_events": (metrics.get("visible_ai") or {}).get("events") or 0,
           "visible_tokens": text("visible_ai", "tokens"),
           "model_calls": sum((b.get("calls") or 0) for b in models),
           "input_tokens": model_sum("input_tokens"),
           "cached_input_tokens": model_sum("cached_input_tokens"),
           "output_tokens": model_sum("output_tokens"),
           "reasoning_tokens": model_sum("reasoning_tokens"),
           "tool_calls": counts.get("tool.call", 0),
           "failed_tool_calls": activity.get("failed_tool_calls") or 0,
           "skill_uses": counts.get("skill.used", 0),
           "sub_agents": counts.get("agent.spawn", 0),
           "sessions": activity.get("sessions") or 0,
           "runs": activity.get("runs") or 0,
           "coverage_gaps": activity.get("coverage_gaps") or 0,
           "span_ms": window.get("span_ms"),
           "operations": artifact.get("operations") or 0,
           "retained": artifact.get("retained") or 0,
           "task_attempts": task.get("attempts") or 0,
           "task_completed": task.get("completed") or 0,
           "files_changed": scope.get("files") or 0,
           "priced_calls": metrics.get("priced_calls") or 0,
           "unpriced_calls": metrics.get("unpriced_calls") or 0,
           "actual_priced_calls": metrics.get("actual_priced_calls") or 0,
           "reference_usd": metrics.get("reference_usd_known_subtotal"),
           "actual_usd": metrics.get("actual_usd_known_subtotal"),
           "skills": sorted(metrics.get("skills_used") or []),
           "mcp": sorted(metrics.get("mcp_used") or [])}
    result = proof.get("score") or None
    row["score"] = None if not result else {"value": result["value"], "grade": result.get("grade"),
                                            "algorithm": result.get("algorithm"),
                                            "confidence": result.get("confidence"),
                                            "cohort": result.get("cohort")}
    return row


def iteration_step(previous, contribution):
    """T = T-1 + this commit. The previous row is the only history this needs."""
    from decimal import Decimal, localcontext
    before = (previous or {}).get("cumulative") or {}
    cumulative = {}
    for field in SUMMABLE:
        cumulative[field] = _add(before.get(field, 0), contribution.get(field))
    with localcontext() as context:
        context.prec = 50
        for field in MONEY:
            carried = Decimal(before.get(field) or 0)
            added = contribution.get(field)
            cumulative[field] = format(carried + Decimal(added), "f") if added is not None else (
                before.get(field) if before.get(field) is not None else None)
        score_total = Decimal((before.get("score_total") or "0"))
        if contribution.get("score"):
            score_total += Decimal(contribution["score"]["value"])
    cumulative["score_total"] = format(score_total, ".1f")
    cumulative["commits"] = (before.get("commits") or 0) + 1
    cumulative["scored_commits"] = (before.get("scored_commits") or 0) + (1 if contribution.get("score") else 0)
    cumulative["skills"] = sorted(set(before.get("skills") or []) | set(contribution.get("skills") or []))
    cumulative["mcp"] = sorted(set(before.get("mcp") or []) | set(contribution.get("mcp") or []))
    cumulative["algorithms"] = sorted(set(before.get("algorithms") or [])
                                      | ({contribution["score"]["algorithm"]} if contribution.get("score") else set()))
    cumulative["timed_commits"] = (before.get("timed_commits") or 0) + (1 if contribution.get("span_ms") is not None else 0)
    return cumulative


def cumulative_view(cumulative):
    """Present a cumulative row with its ratios recomputed from the pooled totals."""
    def ratio(kept, observed):
        return None if not observed else format(kept / observed, ".4f")
    view = {key: cumulative.get(key) for key in SUMMABLE}
    view.update(commits=cumulative.get("commits", 0), scored_commits=cumulative.get("scored_commits", 0),
                timed_commits=cumulative.get("timed_commits", 0),
                skills_used=len(cumulative.get("skills") or []), mcp_used=len(cumulative.get("mcp") or []),
                algorithms=cumulative.get("algorithms") or [],
                reference_usd_known_subtotal=cumulative.get("reference_usd"),
                actual_usd_known_subtotal=cumulative.get("actual_usd"),
                reference_cost_complete=bool(cumulative.get("priced_calls")) and not cumulative.get("unpriced_calls"),
                artifact_operations=cumulative.get("operations"), artifact_retained=cumulative.get("retained"),
                artifact_survival=ratio(cumulative.get("retained") or 0, cumulative.get("operations") or 0),
                task_fulfillment=ratio(cumulative.get("task_completed") or 0, cumulative.get("task_attempts") or 0),
                score_total=cumulative.get("score_total"))
    return view


def lifetime_start():
    """Accumulator for pooled repository totals; fold one proof at a time."""
    from decimal import Decimal
    return {"commits": 0, "scored_commits": 0, "human_tokens": 0, "prompts": 0,
            "visible_tokens": 0, "visible_events": 0, "model_calls": 0,
            "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0,
            "tool_calls": 0, "sub_agents": 0, "skills": set(), "mcp": set(),
            "artifact_operations": 0, "artifact_retained": 0,
            "task_attempts": 0, "task_completed": 0, "priced_calls": 0,
            "unpriced_calls": 0, "cost": Decimal(0), "algorithms": set(),
            "span_ms": 0, "timed_commits": 0, "sessions": 0, "runs": 0,
            "failed_tool_calls": 0, "coverage_gaps": 0,
            "paid": Decimal(0), "actual_priced_calls": 0}


def lifetime_add(totals, proof):
    """Add one recorded commit. Unknown stays unknown; it never becomes zero."""
    from decimal import Decimal, localcontext
    metrics = proof.get("summary") or {}
    totals["commits"] += 1
    for field, bucket, count in (("human_tokens", "human", "prompts"),
                                 ("visible_tokens", "visible_ai", "visible_events")):
        text = metrics.get(bucket) or {}
        tokens = None if text.get("tokens_complete") is False else (
            (text.get("tokens_measured") or 0) + (text.get("tokens_estimated") or 0))
        totals[field] = _add(totals[field], tokens)
        totals[count] += text.get("messages", text.get("events", 0)) or 0
    for model in (metrics.get("models") or {}).values():
        totals["model_calls"] += model.get("calls") or 0
        for field in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens"):
            totals[field] = _add(totals[field], model.get(field))
    counts = metrics.get("event_counts") or {}
    totals["tool_calls"] += counts.get("tool.call", 0)
    totals["sub_agents"] += counts.get("agent.spawn", 0)
    totals["skills"].update(metrics.get("skills_used") or [])
    totals["mcp"].update(metrics.get("mcp_used") or [])
    totals["priced_calls"] += metrics.get("priced_calls") or 0
    totals["unpriced_calls"] += metrics.get("unpriced_calls") or 0
    window, activity = metrics.get("window") or {}, metrics.get("activity") or {}
    if window.get("span_ms") is not None:
        totals["span_ms"] += window["span_ms"]
        totals["timed_commits"] += 1
    for field in ("sessions", "runs", "failed_tool_calls", "coverage_gaps"):
        totals[field] += activity.get(field) or 0
    totals["actual_priced_calls"] += metrics.get("actual_priced_calls") or 0
    with localcontext() as context:
        context.prec = 50
        if metrics.get("reference_usd_known_subtotal") is not None:
            totals["cost"] += Decimal(metrics["reference_usd_known_subtotal"])
        if metrics.get("actual_usd_known_subtotal") is not None:
            totals["paid"] += Decimal(metrics["actual_usd_known_subtotal"])
    result = proof.get("score")
    if not result:
        return totals
    totals["scored_commits"] += 1
    totals["algorithms"].add(result.get("algorithm"))
    evidence = result.get("evidence") or {}
    artifact, task = evidence.get("artifact") or {}, evidence.get("task") or {}
    totals["artifact_operations"] += artifact.get("operations") or 0
    totals["artifact_retained"] += artifact.get("retained") or 0
    totals["task_attempts"] += task.get("attempts") or 0
    totals["task_completed"] += task.get("completed") or 0
    return totals


def lifetime_finish(totals):
    """Recompute ratios from pooled numerators and denominators.

    Averaging per-commit percentages would give a one-line commit the same
    weight as a large one, so the ratios are always pooled, never averaged.
    """
    def ratio(kept, observed):
        return None if not observed else format(kept / observed, ".4f")
    result = {key: value for key, value in totals.items()
              if key not in {"skills", "mcp", "cost", "paid", "algorithms"}}
    result.update(skills_used=len(totals["skills"]), mcp_used=len(totals["mcp"]),
                  actual_usd_known_subtotal=format(totals["paid"], "f") if totals["actual_priced_calls"] else None,
                  reference_usd_known_subtotal=format(totals["cost"], "f") if totals["priced_calls"] else None,
                  reference_cost_complete=bool(totals["priced_calls"]) and not totals["unpriced_calls"],
                  artifact_survival=ratio(totals["artifact_retained"], totals["artifact_operations"]),
                  task_fulfillment=ratio(totals["task_completed"], totals["task_attempts"]),
                  algorithms=sorted(a for a in totals["algorithms"] if a))
    return result


def lifetime(proofs):
    """Pooled physical totals across recorded commits."""
    totals = lifetime_start()
    for proof in proofs:
        lifetime_add(totals, proof)
    return lifetime_finish(totals)


def history_stats(current, history):
    comparable = [p for p in history if p.get("score") and p["score"]["algorithm"] == current["algorithm"]]
    values = [float(p["score"]["value"]) for p in comparable]
    peers = [p for p in comparable if p["score"]["cohort"] == current["cohort"]]
    value = float(current["value"])
    percentile = (sum(float(p["score"]["value"]) < value for p in peers)
                  + .5 * sum(float(p["score"]["value"]) == value for p in peers)) / len(peers) * 100 if peers else None
    return {"count": len(values), "average": round(sum(values) / len(values), 1) if values else None,
            "delta": round(value - values[0], 1) if values else None,
            "peer_count": len(peers), "peer_average": round(sum(float(p["score"]["value"]) for p in peers) / len(peers), 1) if peers else None,
            "percentile": round(percentile, 1) if percentile is not None and len(peers) >= 3 else None,
            "same_grade": sum(p["score"]["grade"] == current["grade"] for p in comparable)}


def ladder_step(previous, commit_score):
    """Add the existing commit score, unchanged, to a zero-based total."""
    from decimal import Decimal, localcontext
    before = Decimal(previous["value"]) if previous else Decimal(0)
    delta = Decimal(commit_score["value"]) if commit_score else Decimal(0)
    with localcontext() as context:
        context.prec = 50
        total = before + delta
    return {"algorithm": "commit-sum-v1", "value": format(total, ".1f"),
            "previous": format(before, ".1f"), "delta": format(delta, ".1f"),
            "rated_commits": (previous["rated_commits"] if previous else 0) + int(commit_score is not None),
            "starting_rating": 0}
