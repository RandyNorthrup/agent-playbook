Fixtures for the media tool tests. Nothing binary: images, audio and video are made in the tests
(.gitignore excludes *.png).

- `video/scenes.json`, `video/words.json`: a made-up five-scene video (one labelled scene, one with the
  character, the last an end card) and the 14 narration words of its texts. It passes `scenes.py lint --strict`.
- `prompts/scenes.json`: four scenes, one per shape the prompt builder branches on (plain, labelled, with the
  character, both).
- `prompts/<channel>.txt`, `prompts/blueprint.<branch>.txt`: the prompt `scenes.py`'s `build_prompt()` returns
  for each of those four scenes with the demo channels in `agent-playbook/media/channels/`, one per line (one
  file per branch for a channel with branches). The parity test compares the builder's output with these lines
  character for character; a deliberate style change updates the channel file and its lines together.
