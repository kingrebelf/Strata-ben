"""The Decisions API: a Jev-compatible `/v1/systemone` (and `/v1/decide` alias).

Jev (TypeSafe AI's System One model) answers typed questions about a `state` -
`choice`, `score` and `noul` - with a probability distribution and a confidence
instead of writing text.  Strata has one model, so these endpoints emulate Jev
with it: one structured generation returns a JSON object that answers every
question, then the module normalizes what the model wrote into Jev's wire
format.  The returned probabilities are the model's own estimates (they are not
a trained calibrator's): gate on `confidence`, or set your own thresholds, as
Jev's own docs suggest.

Request:
  {"model": "jev-latest" (optional), "state": str|object|array,
   "questions": {"name": {"type": "choice"|"score"|"noul", "instructions": str,
                          "criteria": ...}}}

Response:
  {"model": ..., "answers": {"name": {...}}, "usage": {...}}
"""

import json

from serve.structured import StructuredOutputError

CHOICE_MAX = 255
SCORE_MIN_LEVELS, SCORE_MAX_LEVELS = 2, 10
MAX_LONG_QUESTION_TOKENS = 32000
MAX_TOTAL_TOKENS = 64000

SYSTEM = (
    "You are a decision engine, not a chat assistant.  You read a STATE and answer typed "
    "QUESTIONS about it.  Reply with exactly one JSON object and nothing else: an \"answers\" "
    "object with one entry per question name.  Probabilities are decimals from 0 to 1.  A "
    "pick-one answer's probabilities sum to 1; a score answer's per-level probabilities sum "
    "to 1; a yes/no answer's value is the probability the answer is yes.  Read each question "
    "literally - answer the question that is written, not one you might infer - and base the "
    "answer only on the STATE, never on what usually happens."
)


class _Assert:
    def __init__(self, name):
        self.name = name

    def ok(self, ok, what):
        if not ok:
            raise ValueError(f"decisions.{self.name}: {what}")


def check_request(req):
    if not isinstance(req, dict):
        raise ValueError("send a JSON object")
    if "state" not in req:
        raise ValueError("decisions: missing required field state")
    state = req["state"]
    if not isinstance(state, (str, dict, list)):
        raise ValueError("decisions.state: a string, object or array")
    questions = req.get("questions")
    if not isinstance(questions, dict) or not questions:
        raise ValueError("decisions.questions: a non-empty object of named questions")
    out = []
    for name, q in questions.items():
        a = _Assert(f"questions.{name}")
        a.ok(isinstance(name, str) and name.strip(), "every question name is a non-empty string")
        a.ok(isinstance(q, dict), "an object with type, instructions and criteria")
        qtype = q.get("type")
        a.ok(qtype in ("choice", "score", "noul"), "type: choice, score or noul")
        instructions = q.get("instructions")
        a.ok(isinstance(instructions, str) and instructions.strip(),
             "instructions: required descriptive text")
        criteria = q.get("criteria")
        if qtype == "choice":
            a.ok(isinstance(criteria, dict) and criteria, "criteria: a map of options to descriptions")
            a.ok(len(criteria) <= CHOICE_MAX, f"criteria: at most {CHOICE_MAX} options")
            labels, descriptions = [], {}
            for label, desc in criteria.items():
                a.ok(isinstance(label, str) and label, "every option label is a non-empty string")
                a.ok(desc is None or isinstance(desc, str),
                     f"criteria.{label}: null or a description string")
                labels.append(label)
                descriptions[label] = desc
            out.append({"name": name, "type": "choice", "instructions": instructions,
                        "labels": labels, "descriptions": descriptions})
        elif qtype == "score":
            a.ok(isinstance(criteria, list) and SCORE_MIN_LEVELS <= len(criteria) <= SCORE_MAX_LEVELS,
                 f"criteria: an ordered list of {SCORE_MIN_LEVELS} to {SCORE_MAX_LEVELS} levels, low to high")
            a.ok(all(isinstance(level, str) for level in criteria),
                 "criteria: every level is a description string")
            out.append({"name": name, "type": "score", "instructions": instructions,
                        "levels": list(criteria)})
        else:
            if criteria is None:
                criteria = {}
            a.ok(isinstance(criteria, dict) and set(criteria) <= {"true", "false"},
                 "criteria: {\"true\"} and/or {\"false\"} descriptions")
            for key, desc in criteria.items():
                a.ok(desc is None or isinstance(desc, str), f"criteria.{key}: null or a description string")
            out.append({"name": name, "type": "noul", "instructions": instructions, "criteria": dict(criteria)})
    return state, out


def _state_text(state):
    return state if isinstance(state, str) else json.dumps(state, ensure_ascii=False)


def _question_text(q):
    return f"{q['name']} {q['type']} {q['instructions']} " + json.dumps(
        q.get("descriptions") or q.get("levels") or q.get("criteria") or {}, ensure_ascii=False)


def _tokens(text, count):
    if count is not None:
        try:
            n = count(text)
            if n:
                return n
        except Exception:
            pass
    return (len(text) + 3) // 4


def check_budget(state, questions, count=None):
    """Jev's two input budgets: 32k tokens for the state plus the longest question, 64k for the
    state and every question together.  `count` counts tokens (the server's tokenizer) or None
    for the character estimate."""
    state_n = _tokens(_state_text(state), count)
    if state_n + max((_tokens(_question_text(q), count) for q in questions), default=0) > MAX_LONG_QUESTION_TOKENS:
        raise ValueError(f"max_tokens_exceeded: the state plus the longest question is over "
                         f"{MAX_LONG_QUESTION_TOKENS} tokens; trim the state or split the work")
    total = state_n + sum(_tokens(_question_text(q), count) for q in questions)
    if total > MAX_TOTAL_TOKENS:
        raise ValueError(f"max_tokens_exceeded: the state and all questions together are over "
                         f"{MAX_TOTAL_TOKENS} tokens; split the work")


def build_prompt(state, questions):
    lines = ["STATE:", _state_text(state), "", "QUESTIONS:"]
    for i, q in enumerate(questions, 1):
        lines.append(f'{i}. {q["name"]} [{q["type"]}] - {q["instructions"]}')
        if q["type"] == "choice":
            for label in q["labels"]:
                desc = q["descriptions"][label]
                lines.append(f'   option "{label}": {desc}' if desc else f'   option "{label}"')
        elif q["type"] == "score":
            for index, level in enumerate(q["levels"]):
                lines.append(f"   level {index}: {level}")
        else:
            for key, desc in q["criteria"].items():
                lines.append(f'   outcome "{key}": {desc}' if desc else f'   outcome "{key}"')
    return "\n".join(lines) + "\n\nWrite only the JSON object: one answer per question, inside \"answers\"."


def _probability():
    return {"type": "number", "minimum": 0, "maximum": 1}


def build_schema(questions):
    answers = {}
    for q in questions:
        name, qtype = q["name"], q["type"]
        if qtype == "choice":
            labels = q["labels"]
            answers[name] = {
                "type": "object",
                "required": ["type", "choice", "probabilities"],
                "additionalProperties": False,
                "properties": {
                    "type": {"const": "choice"},
                    "choice": {"enum": list(labels)},
                    "probabilities": {
                        "type": "object",
                        "required": list(labels),
                        "additionalProperties": False,
                        "properties": {label: _probability() for label in labels},
                    },
                },
            }
        elif qtype == "score":
            indexes = [str(i) for i in range(len(q["levels"]))]
            answers[name] = {
                "type": "object",
                "required": ["type", "probabilities"],
                "additionalProperties": False,
                "properties": {
                    "type": {"const": "score"},
                    "probabilities": {
                        "type": "object",
                        "required": indexes,
                        "minProperties": len(indexes),
                        "maxProperties": len(indexes),
                        "additionalProperties": False,
                        "properties": {index: _probability() for index in indexes},
                    },
                },
            }
        else:
            answers[name] = {"type": "object", "required": ["type", "noul"],
                             "additionalProperties": False,
                             "properties": {"type": {"const": "noul"}, "noul": _probability()}}
    return {"type": "object", "required": ["answers"], "additionalProperties": False,
            "properties": {"answers": {"type": "object", "required": list(answers),
                                       "additionalProperties": False, "properties": answers}}}


def _distribution(raw, keys):
    if not isinstance(raw, dict):
        raise StructuredOutputError("an answer's probabilities must be an object")
    missing = [key for key in keys if key not in raw]
    if missing:
        raise StructuredOutputError(f"answer is missing probabilities for {missing[0]!r}")
    extra = [k for k in raw if k not in keys]
    if extra:
        raise StructuredOutputError(f"answer has unexpected probability for {extra[0]!r}")
    probs = {}
    for key in keys:
        value = raw[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
            raise StructuredOutputError(f"probability {key!r} must be a number from 0 to 1")
        probs[key] = float(value)
    total = sum(probs.values())
    if total <= 0:
        raise StructuredOutputError("an answer's probabilities do not sum to a positive number")
    normalized = {key: round(value / total, 6) for key, value in probs.items()}
    return normalized, total


def respond(parsed, questions, model, prompt_tokens, completion_tokens):
    if not isinstance(parsed, dict) or not isinstance(parsed.get("answers"), dict):
        raise StructuredOutputError("the model did not return an answers object")
    raw_answers = parsed["answers"]
    answers = {}
    for q in questions:
        a = raw_answers.get(q["name"])
        if not isinstance(a, dict):
            raise StructuredOutputError(f"questions.{q['name']}: no answer returned")
        if a.get("type") != q["type"]:
            raise StructuredOutputError(f"questions.{q['name']}: answer type {a.get('type')!r} is not {q['type']!r}")
        if q["type"] == "choice":
            normalized, _ = _distribution(a.get("probabilities"), q["labels"])
            choice = max(normalized, key=normalized.get)
            confidence = normalized[choice]
            answers[q["name"]] = {"type": "choice", "choice": choice,
                                  "probabilities": normalized, "confidence": confidence}
        elif q["type"] == "score":
            indexes = [str(i) for i in range(len(q["levels"]))]
            normalized, _ = _distribution(a.get("probabilities"), indexes)
            score = sum(i * normalized[index] for i, index in enumerate(indexes))
            confidence = max(normalized.values())
            answers[q["name"]] = {"type": "score", "score": round(score, 6),
                                  "legend": list(q["levels"]),
                                  "probabilities": normalized, "confidence": confidence}
        else:
            value = a.get("noul")
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
                raise StructuredOutputError(f"questions.{q['name']}.noul: a number from 0 to 1")
            answers[q["name"]] = {"type": "noul", "noul": round(float(value), 6)}
    return {"model": model, "answers": answers,
            "usage": {"input_tokens": int(prompt_tokens or 0), "output_tokens": int(completion_tokens or 0)}}