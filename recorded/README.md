# recorded/

What the model step reads, found by the sha256 of each request.

| Folder | What it holds | Who writes it |
|---|---|---|
| `requests/` | `<sha256>.txt`: the exact request text for each invented enquiry that reaches the model | `python -I tools/recordings.py requests` |
| `answers/` | Real model answers, each as it came (`<sha256>.answer.txt`) with its meta file (`<sha256>.json`, `"origin": "recorded"`) | `python -I tools/recordings.py add <sha256> <answer-file> --how "<how it was obtained>"` |
| `constructed/` | One constructed answer per request, written by the tool, not by a model (`"origin": "constructed"`) | `python -I tools/recordings.py construct` |
| `gallery/` | Constructed bad answers for two demo cases: `extra-key/` and `figure-not-in-message/` | `python -I tools/recordings.py construct` |

Every request in `requests/` has a real answer in `answers/`. Each meta file's `how` says how the answer was
obtained: the request text, whole and unchanged, was given as the whole prompt to a Claude Code subagent on
2026-11-24, whose transcript names the model as claude-haiku-5-5. Such a subagent also runs under its harness's
own system prompt, so this is not the live path's API call. The sha256 in each meta file shows only that the
answer has not changed since it was added, and which request it answers. The origin is the author's record.

The desk reads `answers/` first, and a constructed answer only for a request with no recorded one. With every
request recorded, the desk and the demo use no constructed answer; they stay as the fallback, and two demo cases
use the gallery's constructed bad answers.

`python -I tools/recordings.py status` lists every request, the answer the desk would use for it, and what the
desk's own checks make of that answer. A recorded answer is labelled "Recorded" on the desk only when its request
file and answer file both match the sha256s its meta file names.

To record another answer: give the request text, whole and unchanged, as the prompt; save the answer exactly as it
came to a file; then run `add` with that file and a `--how` that says how it was obtained. After adding answers,
run `python -I make_examples.py`, since the examples show which answers the demo used.
