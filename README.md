# Textile Expert

`textile-expert` is an Agent Skill for running an online textile store in Ukraine that imports from Turkey, Italy, China, Germany and Portugal. It works like a technologist and a commercial director in one: fabrics, fibres and densities, choosing and checking suppliers, landed cost and price, quality control and certificates, product cards and SEO in Ukrainian, labelling, returns and import rules. It insists on measurable facts, dates every legal statement, keeps procurement data out of public text, and calculates prices with a script instead of in its head. To install it in Claude Code:

```bash
claude plugin marketplace add dmitriy990/textile-expert-skills
```

```bash
claude plugin install textile-expert@ivatech-textile
```

Then ask in plain language. Claude loads the skill when the task is about textiles, and you can also invoke it by name:

```txt
Write a product card for this sateen bedding set: 70 % cotton, 20 % polyester, 140 g/m², made in Turkey
Calculate landed cost and price: 5,000 m at 2.8 USD, freight 650 USD, rate 41.5, duty 10 %, margin 35 %
Which is better for mid-range terry towels, Turkey or Portugal?
Check this price list before I upload it to the shop
```

| Task | What the skill does |
|---|---|
| **Product cards and feeds** | Ukrainian cards from a template, composition that adds up to 100 %, densities checked against the fabric type, no unproven claims |
| **Suppliers and countries** | Country heuristics, supplier checklist, origin rules for duty, brand country is not production country |
| **Cost and price** | Runs `scripts/landed_cost.py`, shows the assumptions, margin is not markup, a worse-case scenario |
| **Quality control** | Acceptance protocol, test methods, how to verify certificates, claims handling |
| **Legal and customs notes** | Short answers dated "станом на …" with a pointer to the primary source |
| **Assortment and customers** | Reorder points, test batches, replies to customers in Ukrainian |

## Requirements

`python3` on the PATH. The skill needs no connector, no account and no network access: its scripts are standard-library Python and run locally. It has no credentials to configure.

## Automatic checks

In Claude Code the skill registers three small hooks when it is invoked, and they stay active for the rest of the session:

| Hook | When | What it checks |
|---|---|---|
| `lint_content` | after a file is written | composition adds up to 100 %, "bamboo" written as viscose, density fits the fabric, risky washing temperatures, unproven claims, purchase prices and supplier names in public text |
| `price_guard` | before a price file or a shop import is written | negative price, price below cost, margin under the threshold, a "discount" of 50 % or more, outliers such as an extra zero |
| `stop_check` | before an answer is finished | legal statements without a date and a source, cost figures without assumptions |

They are local, offline and fail-open: an internal error never blocks your work. Settings go in `<your project>/.claude/textile-expert.json`; the details, the exceptions and a one-minute check that the hooks run on your machine are in [references/hooks.md](references/hooks.md). To switch them off, run `claude plugin disable textile-expert`.

Hook registration follows Claude Code's documented behaviour for skills. It has not been observed in a live session by the author, which is why that one-minute check exists. Where hooks do not run, the skill runs the same checks by hand before it gives you a result.

## Other ways to install

**Manual copy**

```bash
git clone https://github.com/dmitriy990/textile-expert-skills.git
```

```bash
mkdir -p ~/.claude/skills/textile-expert
```

```bash
cp -R textile-expert-skills/SKILL.md textile-expert-skills/references textile-expert-skills/data textile-expert-skills/hooks textile-expert-skills/scripts ~/.claude/skills/textile-expert/
```

**Claude.ai or Cowork:** copy the same files into a `textile-expert/` folder, delete the `hooks:` block from the frontmatter of `SKILL.md`, zip the folder and upload it as a custom skill. Hooks do not run there, so the skill follows its manual checks. Whether Claude.ai accepts the `hooks:` block was not tested, which is why it is removed.

This is skill version **v1.0.0 (2026-10-09)**.

## What is in the repo

```txt
SKILL.md                     the skill: rules, working processes, response format, hook registration
references/                  seven reference files: fabrics, countries, QC, Ukrainian rules, pricing, content and SEO, hooks
data/                        density ranges per fabric, fibre dictionary, hook defaults
hooks/                       lint_content.py, price_guard.py, stop_check.py
scripts/landed_cost.py       the cost and price calculator
scripts/test_hooks.py        tests for the hooks and the calculator
scripts/validate_package.py  required files, manifests, budgets, hook structure, links, privacy
evals/cases.json             synthetic behavioural cases
evals/JUDGE.md               how to run and judge the cases by hand
.claude-plugin/              plugin and marketplace manifests
```

The reference files are written in Russian, the working language of the author. Everything the skill writes for customers is Ukrainian.

The validator, its self-test and the tests run in CI on pushes to `main`, on version tags and on pull requests, and you can run them yourself:

```bash
python3 scripts/validate_package.py
```

```bash
python3 scripts/validate_package.py --self-test
```

```bash
python3 scripts/test_hooks.py
```

The eval cases are not automated. [evals/JUDGE.md](evals/JUDGE.md) explains how to run them against a copy of the skill that does not contain `evals/`.

## Update

- **Users:** `claude plugin update textile-expert@ivatech-textile`. Auto-update is off by default; you can switch it on per marketplace in `/plugin`.
- **Maintainers:** a release reaches users only when the version changes. The version lives in `SKILL.md` (`metadata.version` and the version header line) and in `.claude-plugin/plugin.json`; the validator checks that they agree and that this README names the header version. Change all of them, run the validator and the tests, then push and tag `v` plus the version.

## Scope and disclaimer

This is a working reference for the Ukrainian market, not legal, customs or tax advice. Rules about labelling, returns, duty, VAT and origin change, and the legal notes in `references/ukraine-legal.md` are dated and cite their sources. Confirm anything that matters with a lawyer, a customs broker or an accountant, and against the primary source (zakon.rada.gov.ua, customs.gov.ua, dpss.gov.ua) before you publish it or rely on it. These notes have not been reviewed by a lawyer.

## License

MIT, see [LICENSE](LICENSE).
