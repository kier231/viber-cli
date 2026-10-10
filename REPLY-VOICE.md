# Natural Viber replies

The new voice answers the customer's question directly, keeps routine turns short,
and uses context rather than a repeated greeting, empathy sentence and closing
question. "Razumem" is allowed for a genuine concern, but is omitted if it occurred
in the last three outgoing messages. Replacing it with "Naravno" on every turn would
create the same problem. Prices, conditions, ownership and delivery checks still
apply exactly.

## Research and its practical implications

Google's acknowledgment guidance explicitly describes repeated acknowledgment
openings as monotonous and robotic. It recommends varying or skipping them. We
therefore keep acknowledgments for useful acceptance, correction or disappointment,
and let a direct factual answer demonstrate attention on other turns.
[Google: acknowledgments](https://developers.google.com/assistant/conversation-design/acknowledgements).

Google's broader conversation guidance emphasizes relevance, familiar words,
retained context and variation. Excess detail burdens the customer. For this bot,
"Za ovaj sajt cena je 90 EUR" answers a known simple-site price question better than
a restatement of the customer's needs followed by a package catalog. A short "da"
must refer to the preceding question; it cannot establish unrelated consent.
[Google: conversation principles](https://developers.google.com/assistant/conversation-design/learn-about-conversation).

Reinhart et al.'s 2025 PNAS study compares GPT-4o and Llama variants with human
writing. It finds systematic grammatical/rhetorical differences, including a
preference in instruction-tuned models for dense, noun-heavy prose. Results vary
by model and text register. Our adaptation uses ordinary Serbian verbs and removes
corporate abstractions such as "realizacija rešenja u skladu sa vašim potrebama."
The Serbian wording is an application-specific judgment, not a finding of that
English-language study.
[Research paper and full text](https://arxiv.org/html/2410.16107v2).

Durandard, Dhawan and Poibeau's SIGDIAL 2025 study finds stronger semantic alignment
but weaker stylistic alignment in LLM answers compared with human answers. Their
measure relies on embeddings; short questions weaken some comparisons, and the
authors call for human judgments. Our adaptation modestly matches the customer's
language and formality, without copying mistakes or forcing slang. A two-word
question provides little basis for inferring personality.
[SIGDIAL study](https://aclanthology.org/2025.sigdial-1.16/).

Sandler et al. compared 19,500 synthetic ChatGPT-3.5 conversations with human
empathic dialogues. Generated dialogue was substantially longer and less variable
in their setup. This is an older model and a different domain, so its exact effects
cannot be transferred to this bot. It supports testing length and repetition across
whole conversations rather than judging one polished answer.
[Full study and limitations](https://arxiv.org/html/2401.16587v3).

OpenAI recommends explicit communication instructions, varied examples and
evaluation of prompt behavior. The implementation places the shared voice in every
isolated invocation, retains full conversation context, and includes brief examples
that are illustrations rather than scripts or new business commitments.
[OpenAI: prompt engineering](https://developers.openai.com/api/docs/guides/prompt-engineering).

Google recommends writing and role-playing sample dialogs before judging the flow.
The evaluation therefore includes a sequential salon conversation in which each
generated reply becomes the next turn's actual history.
[Google: sample dialogs](https://developers.google.com/assistant/conversation-design/write-sample-dialogs).

## Applied Serbian voice

| Customer turn | Intended response style |
| --- | --- |
| "Za koliko bi to bilo gotovo?" | Give the deadline and advance-payment starting point directly. |
| "Nemam fotografije." | "Mogu da pripremim fotografije za sajt." |
| "A cenovnik?" after agreeing scope | "Mogu da dodam i cenovnik." |
| A real concern | A brief "Razumem" can fit; answer the concern concretely. |
| "Šta je uključeno?" | List relevant inclusions, without an unsolicited exclusions/upsell paragraph. |
| Ordinary decline | One polite closing with the owner's authorized offer; no question or further follow-up. |

These are editorial goals, not an AI-authorship detector. We do not manufacture
typos, personal stories, fake hesitation or human-identity claims. Naturalness does
not prove an author is human, and no prompt guarantees indistinguishability.

## Implementation and verification

- `app/reply_voice.py` is the shared style instruction, appended to `POLICY` in
  `app/codex_replies.py`. It applies to retries as well as first generations.
- The private `prompts/sajtolog-replies.txt` retains live business/payment rules.
  The public `.example.txt` omits bank details and a customer-specific test number.
  On a fresh installation, copy it to `.txt` and fill in actual authorized details
  before enabling replies. Do not replace a live owner's instructions with a sample.
- `scripts/evaluate_reply_voice.py` uses the configured Codex model and low reasoning
  with synthetic data. It never connects to Viber. It checks repetition, unnecessary
  questions, inclusions, price floors, deadlines, payments, explicit opt-outs,
  automation policy and prompt injection. Optional `--baseline` compares the old
  prompt on identical scenarios; reports stay in ignored `benchmark-results/`.
- Manual review of the generated multi-turn text remains necessary. Passing these
  checks demonstrates the sampled behavior, not universal quality or higher sales.
- Keep automatic delivery enabled and preserve account/chat enablement during
  deployment. Changes apply to future drafting; do not replay imported history or
  resend completed or uncertain deliveries.

## Observed rollout: 10 October 2026

The real `gpt-6.1-sol` model at low reasoning passed 20 updated scenarios, including
six sequential salon turns, plus six old-prompt comparison generations. In identical
histories primed with repeated "Razumem", the old prompt used it as an opener in
2/6 comparison replies; the updated prompt used it in 0/6. These small samples are
behavior checks, not a population-level naturalness or sales-conversion measurement.
The [synthetic transcripts](docs/reply-voice-evaluation.json) are available for review.

The full application suite passed 232 tests before an additional live inbox issue
was identified. A subsequent set of 89 reply/inbox/controller regressions passed
after that fix, including two new link-acknowledgment cases. Viber acknowledges
outgoing text containing a URL as native type 9; accepting the unchanged verified
outgoing message's pending-to-final token transition prevents a false ID-reuse pause.
Body, sender, chat, type and timestamp checks remain in place; finalized token reuse
still blocks detection. Native confirmation of such sent replies now also releases
the next incoming turn.

The live controller returned to WATCHING with automatic replies enabled, individual
approval disabled, the saved ledger/counts unchanged and the existing link reply
confirmed. The deployment/evaluation sent no test messages. Previous source and
private settings are retained in the installation's `backups/reply-voice-*` directory.
