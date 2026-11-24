# recorded/

What the model step reads, found by the sha256 of each request.

| Folder | What it holds | Who writes it |
|---|---|---|
| `requests/` | `<sha256>.txt`: the exact request text for each invented enquiry that reaches the model | `python -I tools/recordings.py requests` |
| `answers/` | Real model answers, each as it came (`<sha256>.answer.txt`) with its meta file (`<sha256>.json`, `"origin": "recorded"`) | `python -I tools/recordings.py add <sha256> <answer-file> --how "<how it was obtained>"` |
| `constructed/` | One constructed answer per request, written by the tool, not by a model (`"origin": "constructed"`) | `python -I tools/recordings.py construct` |
| `gallery/` | Constructed bad answers for two demo cases: `extra-key/` and `figure-not-in-message/` | `python -I tools/recordings.py construct` |

`python -I tools/recordings.py status` lists every request and whether it has a recorded answer, a constructed one,
or none. An answer is labelled "Recorded" on the desk only when it sits in `answers/` and its request file and
answer file both match the sha256s its meta file names; a constructed answer is labelled "Constructed answer, not
from a model".

To record a real answer: give the request text, whole and unchanged, as the prompt; save the answer exactly as it
came to a file; then run `add` with that file and a `--how` that says how it was obtained. After adding answers,
run `python -I make_examples.py`, since the examples show which answers the demo used.
