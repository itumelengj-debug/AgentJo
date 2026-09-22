# Upgrading an existing install

**Your data is not in the app folder.** Conversations, memories, engines,
settings, job roles, the audit trail and backups all live in:

```
Windows   C:\Users\<you>\.local_agent
macOS     ~/.local_agent
Linux     ~/.local_agent
```

So upgrading is replacing the code beside it. Nothing you have is touched.

## Upgrade

1. **Stop both apps** — close the console windows.

2. **Unzip the new release to a permanent folder.** Not `Downloads`, not
   `%TEMP%`, and **without brackets in the name** — a browser names a second
   download `AgentJo (1)`, and the bracket breaks Windows batch files.
   `C:\AgentJo` is a good choice.

3. **Run `install.bat`** (macOS: double-click `install.command`; Linux:
   `./install.sh`). It finds Python, builds the environment, and checks that
   **both** apps load before saying it's ready.

4. **Delete the old folder** once the new one works. Your data is elsewhere,
   so this only removes code.

## Running both

```
start_agent_jo.bat        →  http://127.0.0.1:8765   the agent
start_agent_jo_jobs.bat   →  http://127.0.0.1:8766   the job search
```

Two windows, one set of data. Open both — they share the same engines,
memories and audit trail, so a role you save in one is the role the other
sees. Neither needs the other running.

macOS and Linux use `start_agent_jo.command` / `.sh` and
`start_agent_jo_jobs.command` / `.sh`.

## Keeping the old one alongside

Unzip the new release somewhere separate and run it from there. Both versions
read the same `.local_agent`, so **they are not isolated** — a change in one
is a change in both. Fine for comparing the interface, not for testing
anything that writes.

To isolate one completely, point it at its own data:

```
set AGENT_HOME=C:\AgentJoTest        (Windows)
export AGENT_HOME=~/agentjo-test     (macOS/Linux)
```

Then it starts empty, with none of your engines or history.

## If something goes wrong

Run `install.bat --check`. It surveys the machine and changes nothing.
`install.log` records every command and its output.

Your data is still in `.local_agent` regardless — reinstalling never touches
it, and the nightly backup (Backup panel) is the one to turn on if you
haven't.
