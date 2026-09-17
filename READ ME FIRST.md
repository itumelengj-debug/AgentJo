# Agent Jo

A private AI agent that runs entirely on this computer.

## Install (once)

Double-click **install.bat**.

It creates a self-contained environment, puts an **Agent Jo** shortcut on the
desktop, and starts the app. It needs Python 3.10+ — if it's missing, the
installer says so and links to it (tick *Add python.exe to PATH* when
installing Python).

## First run

The app asks how it should think:

* **Anthropic (Claude)** — best quality, paid per use. Get a key at
  platform.claude.com. It's sealed on this machine and never sent anywhere
  except Anthropic.
* **Ollama** — free and fully private, nothing leaves the computer. Install
  from ollama.com, then run `ollama pull qwen3`.

Either can be changed later, and both can be used together.

## Where your data lives

Everything — conversations, memories, documents, settings — stays in
`.local_agent` in your user folder. Nothing is uploaded. To remove the app,
delete this folder and that one.

## This copy carries no personal data

It ships with no API key, no conversations and no memories. Whoever shared it
built it with a tool that removes those and refuses to package if any are
found.
