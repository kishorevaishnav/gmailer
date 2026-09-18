# Single sequential summarizer

The app's AI summaries are produced by exactly one background worker thread that
processes hungry messages one at a time, one per second. API endpoints never
generate summaries synchronously on demand; a card expand marks a message urgent
so the worker reorders it to the front of its queue, still serially.

We made this decision because the frontend used to fire up to six parallel
summarize requests on card expand (`SINGLE_SUMM_CONCURRENCY`), competing with the
background worker and hammering the local Ollama model — exactly the
"server slog" the client requirements call out. A single sequential worker bounds
LLM load to one request at a time; the cost is that a just-expanded email may wait
behind earlier hungry messages.