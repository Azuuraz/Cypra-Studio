"""Persistent operational state shared by Director and task inspection."""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "data" / "operations.json"
TASK_STATES = ("created", "analyzed", "assigned", "running", "review", "completed", "failed")
_UNFINISHED = set(TASK_STATES[:5])
_LOCK = threading.RLock()


def _empty() -> dict[str, Any]:
    return {"schema_version": 4, "tasks": {}, "agents": {}, "relationships": {}, "chat_feedback": {}, "evolution_proposals": {}, "updated_at": None}


def _read() -> dict[str, Any]:
    try:
        data = json.loads(PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else _empty()
    except Exception:
        return _empty()


def _write(data: dict[str, Any]) -> None:
    PATH.parent.mkdir(parents=True, exist_ok=True)
    data["schema_version"] = 4
    data["updated_at"] = time.time()
    tmp = PATH.with_name(f"{PATH.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(PATH)


def _agent(data: dict[str, Any], slug: str) -> dict[str, Any]:
    return data.setdefault("agents", {}).setdefault(slug, {
        "assignments": 0, "successful_assignments": 0, "failed_assignments": 0,
        "success_rate": None, "reliability": None, "reward_total": 0.0,
        "feedback_count": 0, "average_reward": None, "domain_scores": {}, "task_history": [],
        "chat_responses": 0, "chat_response_ids": [], "chat_positive": 0,
        "chat_negative": 0, "chat_feedback_count": 0, "chat_score": None,
        "chat_confidence": 0.0, "score": None,
    })


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def _confidence_weighted_rate(successes: int, failures: int) -> tuple[float | None, float, float | None]:
    """Return observed rate, evidence confidence, and conservative score."""
    successes = max(0, int(successes or 0))
    failures = max(0, int(failures or 0))
    attempts = successes + failures
    if not attempts:
        return None, 0.0, None
    observed = successes / attempts
    posterior = (successes + 1.0) / (attempts + 2.0)
    confidence = attempts / (attempts + 4.0)
    score = _clamp(0.5 + (posterior - 0.5) * confidence)
    return observed, confidence, score


def _normalize_relationship(row: dict[str, Any]) -> dict[str, Any]:
    successes = max(0, int(row.get("successful_tasks") or 0))
    failures = max(0, int(row.get("failed_tasks") or 0))
    shared = max(successes + failures, int(row.get("shared_tasks") or 0))
    # Legacy rows only stored successful_tasks and an unbounded count strength.
    if shared > successes + failures:
        failures = max(0, shared - successes)
    observed, confidence, strength = _confidence_weighted_rate(successes, failures)
    row.update({
        "successful_tasks": successes,
        "failed_tasks": failures,
        "shared_tasks": successes + failures,
        "success_rate": observed,
        "confidence": round(confidence, 6),
        "strength": None if strength is None else round(strength, 6),
        "score": None if strength is None else round(strength * 100.0, 2),
        "scored": strength is not None,
    })
    return row


def _refresh_agent_score(metric: dict[str, Any]) -> None:
    successes = max(0, int(metric.get("successful_assignments") or 0))
    failures = max(0, int(metric.get("failed_assignments") or 0))
    observed, confidence, reliability = _confidence_weighted_rate(successes, failures)
    metric["success_rate"] = observed
    metric["reliability"] = reliability
    metric["confidence"] = round(confidence, 6)
    metric["evidence_count"] = successes + failures
    positive = max(0, int(metric.get("chat_positive") or 0))
    negative = max(0, int(metric.get("chat_negative") or 0))
    _, chat_confidence, chat_score = _confidence_weighted_rate(positive, negative)
    metric["chat_feedback_count"] = positive + negative
    metric["chat_success_rate"] = positive / (positive + negative) if positive + negative else None
    metric["chat_confidence"] = round(chat_confidence, 6)
    metric["chat_score"] = chat_score
    weighted: list[tuple[float, int]] = []
    if reliability is not None and successes + failures:
        weighted.append((reliability, successes + failures))
    if chat_score is not None and positive + negative:
        weighted.append((chat_score, positive + negative))
    total_evidence = sum(weight for _, weight in weighted)
    overall = sum(value * weight for value, weight in weighted) / total_evidence if total_evidence else None
    metric["score"] = None if overall is None else round(overall * 100.0, 2)
    metric["overall_evidence_count"] = total_evidence


def record_chat_interaction(slug: str, response_id: str) -> dict[str, Any]:
    """Register a real agent response once so operational evidence can hydrate lazily."""
    slug = str(slug or "").strip().lower()
    response_id = str(response_id or "").strip()
    if not slug or not response_id:
        raise ValueError("Agent and response id are required")
    with _LOCK:
        data = _read()
        metric = _agent(data, slug)
        ids = list(metric.get("chat_response_ids") or [])
        is_new = response_id not in ids
        if is_new:
            ids.append(response_id)
            metric["chat_response_ids"] = ids[-500:]
            metric["chat_responses"] = int(metric.get("chat_responses") or 0) + 1
        _refresh_agent_score(metric)
        if is_new:
            _write(data)
        return dict(metric)


def record_chat_feedback(response_id: str, slug: str, sentiment: int) -> dict[str, Any]:
    """Upsert one positive/negative vote and recompute that agent from evidence."""
    response_id = str(response_id or "").strip()
    slug = str(slug or "").strip().lower()
    sentiment = 1 if int(sentiment) > 0 else -1 if int(sentiment) < 0 else 0
    if not response_id or not slug:
        raise ValueError("Response id and agent are required")
    with _LOCK:
        data = _read()
        records = data.setdefault("chat_feedback", {})
        previous = records.get(response_id)
        if previous and str(previous.get("agent") or "") != slug:
            raise ValueError("Feedback response is already attributed to another agent")
        if sentiment:
            records[response_id] = {"agent": slug, "sentiment": sentiment, "updated_at": time.time()}
        else:
            records.pop(response_id, None)
        metric = _agent(data, slug)
        relevant = [row for row in records.values() if str(row.get("agent") or "") == slug]
        metric["chat_positive"] = sum(1 for row in relevant if int(row.get("sentiment") or 0) > 0)
        metric["chat_negative"] = sum(1 for row in relevant if int(row.get("sentiment") or 0) < 0)
        _refresh_agent_score(metric)
        _write(data)
        return {"agent": slug, "sentiment": sentiment, "metrics": dict(metric)}


def analyze_workforce(*, minimum_attempts: int = 5, weakness_threshold: float = 0.75) -> dict[str, Any]:
    """Detect evidence-backed domain gaps without creating or rewriting an agent."""
    with _LOCK:
        data = _read(); aggregates: dict[str, dict[str, Any]] = {}
        for slug, metric in (data.get("agents") or {}).items():
            for domain, row in (metric.get("domain_scores") or {}).items():
                attempts = max(0, int(row.get("attempts") or 0)); successes = max(0, int(row.get("successes") or 0))
                if not attempts: continue
                target = aggregates.setdefault(str(domain), {"attempts": 0, "successes": 0, "agents": []})
                target["attempts"] += attempts; target["successes"] += successes
                target["agents"].append({"slug": slug, "attempts": attempts, "successes": successes, "score": successes / attempts})
        proposals = data.setdefault("evolution_proposals", {}); observed: list[dict[str, Any]] = []
        for domain, row in aggregates.items():
            attempts = int(row["attempts"]); success_rate = row["successes"] / attempts if attempts else None
            evidence_confidence = attempts / (attempts + 4.0) if attempts else 0.0
            # Require the confidence-adjusted upper estimate to remain below the
            # weakness threshold. This avoids proposing workforce changes from a
            # marginal 3/5 or similarly noisy sample.
            weakness_upper = min(1.0, float(success_rate or 0.0) + (1.0 - evidence_confidence) * 0.25)
            if attempts < minimum_attempts or success_rate is None or weakness_upper >= weakness_threshold: continue
            proposal_id = "cap-" + re.sub(r"[^a-z0-9]+", "-", domain.lower()).strip("-")[:48]
            existing = proposals.get(proposal_id) or {}; domain_agents = {a["slug"] for a in row["agents"]}; best_pair = None
            for relationship in (data.get("relationships") or {}).values():
                if {relationship.get("agent_a"), relationship.get("agent_b")} <= domain_agents and int(relationship.get("shared_tasks") or 0) > 0:
                    if best_pair is None or int(relationship.get("shared_tasks") or 0) > int(best_pair.get("shared_tasks") or 0): best_pair = dict(relationship)
            proposal = {
                "id": proposal_id, "kind": "capability_profile", "domain": domain,
                "title": " ".join(part.capitalize() for part in re.split(r"[-_ ]+", domain) if part) + " Specialist",
                "status": existing.get("status") or "pending", "created_at": existing.get("created_at") or time.time(), "updated_at": time.time(),
                "observed_weakness": True,
                "evidence": {"attempts": attempts, "successes": int(row["successes"]), "success_rate": round(success_rate, 6), "confidence": round(evidence_confidence, 6), "weakness_upper": round(weakness_upper, 6), "agents": sorted(row["agents"], key=lambda a: (-a["attempts"], a["slug"]))[:8], "best_pair": best_pair},
                "recommendation": f"Create a focused {domain} capability profile for user review.", "created_agent": existing.get("created_agent"),
            }
            proposals[proposal_id] = proposal; observed.append(proposal)
        data["workforce_analysis"] = {"analyzed_at": time.time(), "domains_observed": len(aggregates), "weaknesses": len(observed), "minimum_attempts": minimum_attempts, "weakness_threshold": weakness_threshold}
        _write(data)
        return {"analysis": data["workforce_analysis"], "proposals": sorted(proposals.values(), key=lambda p: p.get("updated_at") or 0, reverse=True)}


def set_evolution_proposal_status(proposal_id: str, status: str, *, created_agent: str | None = None) -> dict[str, Any]:
    if status not in {"approved", "rejected"}: raise ValueError("Invalid evolution decision")
    with _LOCK:
        data = _read(); proposal = data.setdefault("evolution_proposals", {}).get(proposal_id)
        if not proposal: raise ValueError("Evolution proposal not found")
        if proposal.get("status") not in {"pending", status}: raise ValueError("Evolution proposal already decided")
        proposal["status"] = status; proposal["decided_at"] = time.time(); proposal["updated_at"] = time.time()
        if created_agent: proposal["created_agent"] = created_agent
        _write(data); return dict(proposal)


def create_task(task_id: str, description: str, **fields: Any) -> dict[str, Any]:
    with _LOCK:
        data = _read(); now = time.time()
        task = {"id": task_id, "description": description, "status": "created", "created_at": now,
                "updated_at": now, "assigned_agents": [], "model": None, "duration": None,
                "result_reference": None, "errors": [], "retries": 0, "reward": None,
                "feedback": None, "routing_decision": None, "interrupted": False,
                "state_history": [{"status": "created", "at": now}], **fields}
        data.setdefault("tasks", {})[task_id] = task; _write(data); return task


def update_task(task_id: str, status: str | None = None, **fields: Any) -> dict[str, Any] | None:
    if status is not None and status not in TASK_STATES:
        raise ValueError(f"Invalid task state: {status}")
    with _LOCK:
        data = _read(); task = data.setdefault("tasks", {}).get(task_id)
        if not task: return None
        if status is not None:
            previous_status = str(task.get("status") or "")
            task["status"] = status
            if status != previous_status:
                history = list(task.get("state_history") or [])
                history.append({"status": status, "at": time.time()})
                task["state_history"] = history[-32:]
        task.update(fields); task["updated_at"] = time.time()
        if status in {"completed", "failed"}:
            task["finished_at"] = task["updated_at"]
            task["duration"] = max(0.0, task["finished_at"] - float(task.get("created_at") or task["finished_at"]))
        _write(data); return task


def record_assignment(
    task_id: str,
    agents: list[str],
    domains: list[str] | None = None,
    *,
    collaborators: list[str] | None = None,
) -> None:
    clean = list(dict.fromkeys(str(a) for a in agents if a))
    clean_domains = list(dict.fromkeys(str(d).strip().lower() for d in domains or [] if str(d).strip()))
    participants = list(dict.fromkeys(str(a) for a in (collaborators or clean) if a))
    with _LOCK:
        data = _read(); task = data.setdefault("tasks", {}).get(task_id)
        if not task: return
        previous = set(task.get("assigned_agents") or [])
        task["assigned_agents"] = clean
        task["collaborating_agents"] = participants
        task["domains"] = clean_domains
        task["assignment_evidence"] = {
            "assigned_agents": clean,
            "collaborating_agents": participants,
            "domains": clean_domains,
            "recorded_at": time.time(),
        }
        for slug in clean:
            metric = _agent(data, slug)
            if slug not in previous:
                metric["assignments"] += 1
                metric["task_history"] = (metric.get("task_history", []) + [task_id])[-100:]
            for domain in clean_domains:
                metric.setdefault("domain_scores", {}).setdefault(domain, {"successes": 0, "attempts": 0, "score": None})
        for slug in participants:
            _agent(data, slug)
        _write(data)


def record_outcome(task_id: str, success: bool, *, reward: float | None = None, feedback: Any = None,
                   result_reference: str | None = None) -> None:
    with _LOCK:
        data = _read(); task = data.setdefault("tasks", {}).get(task_id)
        if not task: return
        if task.get("outcome_recorded"):
            return
        task.update({"reward": reward, "feedback": feedback, "result_reference": result_reference})
        task["outcome_recorded"] = True
        task["outcome"] = "success" if success else "failure"
        task["outcome_recorded_at"] = time.time()
        agents = task.get("assigned_agents") or []
        domains = task.get("domains") or []
        for slug in agents:
            metric = _agent(data, slug)
            key = "successful_assignments" if success else "failed_assignments"; metric[key] += 1
            _refresh_agent_score(metric)
            if reward is not None:
                metric["reward_total"] += float(reward); metric["feedback_count"] += 1
                metric["average_reward"] = metric["reward_total"] / metric["feedback_count"]
            for domain in domains:
                row = metric.setdefault("domain_scores", {}).setdefault(domain, {"successes": 0, "attempts": 0, "score": None})
                row["attempts"] += 1
                if success: row["successes"] += 1
                row["score"] = row["successes"] / row["attempts"]
        participants = list(dict.fromkeys(task.get("collaborating_agents") or agents))
        if len(participants) > 1:
            for i, left in enumerate(participants):
                for right in participants[i + 1:]:
                    if left == right:
                        continue
                    key = "|".join(sorted((left, right)))
                    rel = data.setdefault("relationships", {}).setdefault(key, {
                        "agent_a": min(left, right), "agent_b": max(left, right),
                        "successful_tasks": 0, "failed_tasks": 0,
                    })
                    outcome_key = "successful_tasks" if success else "failed_tasks"
                    rel[outcome_key] = int(rel.get(outcome_key) or 0) + 1
                    rel["last_task_id"] = task_id
                    rel["last_outcome"] = "success" if success else "failure"
                    _normalize_relationship(rel)
        _write(data)


def performance_score(
    slug: str,
    query_tokens: set[str],
    team: list[str] | None = None,
    *,
    operational_data: dict[str, Any] | None = None,
) -> tuple[float, dict[str, Any]]:
    """Score one agent; roster routing can provide one loaded snapshot."""
    data = operational_data if isinstance(operational_data, dict) else _read()
    metric = data.get("agents", {}).get(slug) or {}
    domain = 0.0
    for name, row in (metric.get("domain_scores") or {}).items():
        if set(str(name).lower().split()) & query_tokens and row.get("score") is not None: domain = max(domain, float(row["score"]))
    reliability = metric.get("reliability"); reward = metric.get("average_reward"); chat = metric.get("chat_score")
    relationship_values: list[float] = []
    for mate in team or []:
        rel = data.get("relationships", {}).get("|".join(sorted((slug, mate)))) or {}
        normalized = _normalize_relationship(dict(rel))
        if normalized.get("strength") is not None:
            relationship_values.append(float(normalized["strength"]))
    relationship = sum(relationship_values) / len(relationship_values) if relationship_values else None
    reward_signal = _clamp(float(reward), -1.0, 1.0) if reward is not None else None
    bonus = domain * 8 + (float(reliability) * 10 if reliability is not None else 0) + (float(chat) * 4 if chat is not None else 0) + (reward_signal * 2 if reward_signal is not None else 0) + (relationship * 3 if relationship is not None else 0)
    return bonus, {"domain": domain or None, "reliability": reliability, "chat_reputation": chat, "prior_success": metric.get("success_rate"), "reward": reward, "team_history": relationship}


def snapshot(*, mark_interrupted: bool = False) -> dict[str, Any]:
    with _LOCK:
        data = _read()
        data["schema_version"] = 4
        if mark_interrupted:
            changed = False
            for task in data.get("tasks", {}).values():
                if task.get("status") in _UNFINISHED and not task.get("interrupted"):
                    task["interrupted"] = True; task["interrupted_at"] = time.time(); changed = True
            if changed: _write(data)
        for metric in data.get("agents", {}).values():
            _refresh_agent_score(metric)
        for rel in data.get("relationships", {}).values():
            _normalize_relationship(rel)
        ranking = sorted(
            data.get("agents", {}),
            key=lambda s: (
                data["agents"][s].get("score") is None,
                -(data["agents"][s].get("score") if data["agents"][s].get("score") is not None else -1),
                -int(data["agents"][s].get("evidence_count") or 0),
                s,
            ),
        )
        data["preferred_agent_ranking"] = ranking
        return data
