# gptfree-register protocol core

This directory contains the minimal Mail Auth protocol runtime vendored from
[`hyhang915/gptfree-register`](https://github.com/hyhang915/gptfree-register) at
commit `ef34569cabf4da011dfb27bdd04617022e227bb8`.

Included files:

- `core/chatgpt_register.py`
- `core/sentinel_token.py`
- `core/codex_oauth.py`
- `core/gpt_trial_protocol/*.py`
- `core/gen_token_jsdom.js`
- `core/sentinel_vm/*.js`
- `core/package.json` and `core/package-lock.json`

The upstream project is MIT licensed; its license is preserved as `LICENSE`.
The surrounding Web UI, account pool, and storage files are not included
because `hidemyemail-generator` supplies those responsibilities.
