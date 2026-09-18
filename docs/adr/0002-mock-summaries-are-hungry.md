# Mock summaries are hungry

When the local Ollama model is unreachable, `ai_summary` falls back to a
deterministic mock summary which is then persisted like a real one. We treat a
mock summary as *not done*: the worker keeps retrying the message until it
produces a real (non-mock, one-liner) summary.

The naive reading of "only summarize when the summary is empty" would see the
persisted mock and skip the message forever, silently degrading the whole inbox
to mock summaries whenever Ollama hiccups. Retrying mocks keeps "don't redo it"
true for real summaries while still converging on a genuine one-liner when the
model recovers.