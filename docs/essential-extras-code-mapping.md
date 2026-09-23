# Essential Extras structured category

`get_plan_details(detail_types=["essential_extras"])` returns the following provider benefit codes together, using the existing normalized code matching:

| Provider benefit code | Meaning |
| --- | --- |
| `Essential_Extras` | Program availability |
| `Essential_Extras_Options` | Options and their supplied terms |
| `Essential_Extras_Selections` | Selection limit, such as `Pick 1` |

The category describes the named Essential Extras program. It does not include routine dental, vision, hearing, transportation, OTC, Everyday Options, or paid optional packages. Those retain their existing retrieval paths. The provider's `covered: false` flag does not override an answerable benefit value.

Missing category rows use the existing non-premium document fallback. The existing full-catalog exception still applies: report structured gaps without document fallback. Like other grouped categories, a returned category does not establish terms missing from its rows; the agent must use document evidence for requested missing terms or relationships. No additional completeness validator is introduced.

The existing `response_instructions` field adds Essential Extras grounding guidance only when that category is requested or the trusted current question names the program. This also covers premium-only calls that omit the requested program: fetch its current terms, search documents for a requested relationship, and do not infer that premium and choice limits are independent. Ordinary requests receive no new guidance. Full-catalog requests retain their no-document-fallback rule.

The guidance adapts the named-program attribution and relationship rules in `search_plans`. The existing document-search attribution and unsupported-evidence rules remain intact. Only an Essential Extras document query that asks about a premium relationship receives an appended relationship boundary. Matching uses the normalized document questions and requires relationship wording; a name such as Premium Savings alone does not trigger it.

The scoped response instructions include concrete distinctions for ordinary OTC/dental allowances, transportation exclusions, and paid packages. When program membership is unsupported, the answer must leave it unconfirmed in both directions: missing evidence does not establish inclusion, exclusion, separation, or independence. Supported structured program facts remain usable. These instructions do not add a semantic classifier, change retrieval, or require a system-prompt edit.
