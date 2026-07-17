# Model Benchmark (2026-04-04)

Test video: "7 Techniques That Make Claude Code Websites Look $10,000" (18 min, ID: jhIV97AZ45M)

All models tested via OpenRouter API with identical prompt (summary generation only, not full pipeline).

| | DeepSeek v3.2 | GPT-4o-mini | Claude 3.5 Haiku |
|---|---|---|---|
| **Time** | ~330s (5.5 min) | 23.7s | 14.1s |
| **Input tokens** | 5,213 | 4,461 | 4,740 |
| **Output tokens** | 9,673 | 1,061 | 589 |
| **Output size** | ~30,000 chars | 4,060 chars | 2,467 chars |
| **Cost** | $0.005 | $0.001 | $0.006 |
| **Sections covered** | All 10 chapters | 8 techniques | 8 techniques |
| **Timestamp links** | Correct YouTube links | Broken anchor hrefs | Plain text, no links |

## Speed

Haiku is 23x faster than DeepSeek. GPT-4o-mini is 14x faster.

## Quality

DeepSeek produced the most detailed, useful output by far (9,673 output tokens vs 1,061 and 589). Both alternatives covered the same topics but with far less depth. GPT-4o-mini included a table of contents but used broken anchor-based links instead of YouTube URLs. Haiku was extremely sparse and used plain text timestamps with no links at all.

## Cost

GPT-4o-mini is cheapest at $0.001 per summary. Haiku is actually more expensive than DeepSeek ($0.006 vs $0.005) because Anthropic's per-token pricing is higher, even though it generated fewer tokens.

## Decision

Staying with DeepSeek v3.2. The quality difference is significant for content that will be referenced for months. The extra processing time (5 minutes vs 15-25 seconds) is acceptable for a tool that runs occasionally, not interactively.
