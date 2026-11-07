# Wiki: Add Your Own LLM API Keys (BYOK)

> Status: this describes the **planned** feature (see [roadmap](../roadmap.md)). Provider
> consoles and model names change often, so check each provider's current docs.

SchemaDrill is free, so it does not pay for your AI usage. You bring your own API key, and
requests to the model are billed to **your** provider account.

## Supported providers (planned order)

| Provider | Get a key from | Notes |
| --- | --- | --- |
| Google Gemini | Google AI Studio | First supported provider |
| Anthropic Claude | Anthropic Console | Planned |
| OpenAI GPT | OpenAI Platform | Planned |
| Others | OpenRouter, Mistral, Groq, any OpenAI-compatible endpoint | Later |

## How to add a key

1. Create an API key in your provider's console (billing may be required for some providers).
2. In SchemaDrill, open **Settings > LLM keys** and click **Add key**.
3. Choose the provider, paste the key, give it a label (for example "personal Gemini"), and pick
   a default model.
4. SchemaDrill makes a small test call to validate it, then stores it **encrypted**.
5. After saving, the key is never shown again. You will only see the provider, label, last
   4 characters, and status.

## Several keys and fallback

You can save multiple keys and mark one as the default. You can also set an order, so if the
first key fails, SchemaDrill automatically tries the next.

## What happens when a key stops working

| Problem | Status | What SchemaDrill does |
| --- | --- | --- |
| Invalid, revoked, or expired key | `invalid` | Stops using it and asks you to replace it |
| Rate limited | `rate_limited` | Retries with backoff, then tries your next key |
| Out of quota or credits | `out_of_quota` | Pauses that key and tells you which one ran out |
| Provider outage | `degraded` | Retries, then tries your next key |

**Nothing is lost:** if a question fails because of a key problem, it stays on screen. Fix or
replace the key, then press **Retry** to get the answer without retyping.

## Security

- Keys are encrypted at rest and never written to logs.
- The API cannot return a saved key. To change one, replace or delete it.
- Delete a key from Settings at any time. It is removed from storage immediately.
- Also consider setting a spending limit in your provider's console.
