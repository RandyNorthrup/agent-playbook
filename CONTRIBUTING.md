# Contributing

Thanks for helping. Bug reports, fixes, new QC filter types, helper scripts and documentation are all welcome.

## Before you start

- For anything bigger than a small fix, open an issue first so we can agree on the approach.
- The engine and the helper scripts use the Python standard library only and must run on Python 3.8 or newer,
  on Windows, macOS and Linux. Media tools may use Pillow, numpy and ffmpeg, and must say so and exit 3 when
  one is missing.
- The canvas is one self-contained page with no external JavaScript.

## Making a change

1. Fork, then branch from `main`.
2. Write a failing test first where you can. Tests run on fixture playbooks in `tests/fixtures/` and never touch
   the shipped store or the network.
3. Keep command-line flags, exit codes (0 done, 1 refused or failed, 2 bad command line), file formats and the
   server's HTTP API backward compatible: agents and flows depend on them.
4. Run the same checks as CI:

   ```sh
   python -m unittest discover -s tests
   python agent-playbook/playbook/pb.py check --strict
   python agent-playbook/playbook/pb.py normalize --check
   python agent-playbook/playbook/app/build_page.py      # after editing app/playbook.src.html; commit playbook.html too
   python agent-playbook/scripts/make_index.py           # after changing a script's arguments
   python agent-playbook/scripts/make_index.py --dir agent-playbook/media
   ```

5. Open a pull request that says what changed and why, and how you tested it.

## Changing the demo playbook

The shipped store is a fictional demo. Change it through the canvas or `pb.py put`, never by hand, and keep
`pb.py check --strict` at zero problems and zero warnings. Never add real people's names, real accounts or
personal details to it.

## Code of conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md).
