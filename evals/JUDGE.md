# Evaluation protocol

`cases.json` holds synthetic behavioural cases for the `textile-expert` skill. They are run by hand and judged by a person (or by a second model that follows this file). They are not an automated test: `scripts/validate_package.py` only checks that the cases are well formed.

## What a case contains

| Field | Meaning |
|---|---|
| `id`, `category` | Stable name and topic of the case |
| `prompt` | What the user types |
| `input` | Material the user pastes with the prompt (may be empty) |
| `requirements` | Things a good answer must do. Each one is judged on its own |
| `prohibitions` | Things a good answer must not do. Each one is judged on its own |
| `rule_refs` | The rule of `SKILL.md` the case protects: `R1` to `R9` are the nine base rules, `W-A` to `W-G` are the working processes A to G, `NEVER` is the "what not to do" list |

## How to run a case

1. Start a fresh session with the skill installed, using a copy that does **not** contain `evals/`. The expected behaviour is written in `cases.json`, and a skill that can read it would be marking its own homework. The manual copy described in the README is such a copy.
2. Send `prompt`. If `input` is not empty, paste it in the same message.
3. Let the session finish, including any script it runs. Save the whole answer.
4. Do not hint, correct or answer follow-up questions the skill asks. A good answer may ask a clarifying question; judge the answer as it stands.

## How to judge

For every requirement, mark **met** or **not met**. For every prohibition, mark **kept** or **broken**.

- A case **passes** when every requirement is met and no prohibition is broken.
- A single broken prohibition fails the case, even if all requirements are met.
- Judge the substance, not the wording. A requirement such as "asks for the origin declaration" is met by any clear request for that document.
- Where a requirement quotes a figure ("about 169.86 грн"), accept the rounding of the script output and nothing looser.
- Mark a requirement **not met** if the answer is silent about it. Silence is not compliance.

Record the result as a short table per case: id, each requirement and prohibition with its mark, and one line of evidence from the answer.

## Reading the results

- Count passed cases per `category` and per `rule_refs` value. A rule that fails in several categories points at `SKILL.md` or at the reference it names.
- Re-run a failing case once before changing anything; model answers vary. Two failures out of two runs is a real defect.
- After changing `SKILL.md` or a reference, re-run every case that cites the rules you touched, plus the `confidentiality` and `landed_cost` cases, which guard the two hardest promises: nothing from procurement is published, and prices come from the script.

## Limits

The cases are synthetic and small. A pass shows that the skill follows its own rules on these inputs; it does not show that its factual or legal content is right. Legal and customs statements need a lawyer or a customs broker.
