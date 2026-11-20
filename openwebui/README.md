# Open WebUI tool

[`ha_timeline_tool.py`](ha_timeline_tool.py) gives a chat model in Open WebUI two
read-only tools:

- **find_home_assistant_entities**: looks up entity IDs from words like
  "office climate".
- **get_home_assistant_timeline**: pulls and condenses a time window for a list
  of entities. The result is the same timeline the CLI writes to `timeline.txt`.

The model reasons over the timeline in the chat, so this replaces the CLI's
`ask` step. Open WebUI's own model connection is used, so `LLM_*` settings
aren't needed here.

## Install

1. **Make sure Open WebUI can reach Home Assistant.** The tool runs inside
   Open WebUI's server, which is usually a Docker container, not in your
   browser. From that container, `HA_URL` has to resolve. An IP address avoids
   problems with `.local` names inside Docker.
2. **Add the tool.** Go to Workspace → Tools → **+**, paste the whole file, and
   save. On first load, Open WebUI installs the package from the
   `requirements:` line, which downloads this repo's `main` branch from GitHub.
   - That only works while the repo is public. If it's private, install the
     package into the container yourself, for example with
     `docker exec open-webui pip install <path or URL>`.
   - Some installs turn off frontmatter installs with
     `ENABLE_PIP_INSTALL_FRONTMATTER_REQUIREMENTS=false`. Install it yourself
     the same way.
3. **Set the valves.** Click the tool's gear icon and set:
   - `ha_url` and `ha_token`. Use an admin user's token, or the error log
     can't be read.
   - `timezone`, for example `America/New_York`. Without it, times show in the
     container's zone, which is usually UTC.
4. **Turn it on for a model.** Go to Workspace → Models → edit the model →
   tick the tool. Or turn it on per chat from the **+** menu next to the
   message box.

## Using it

Ask the way you'd describe the problem to a person:

> Why didn't the MXZ front coordinator change the office heat in the last two
> hours?

The model should look up the entity IDs, pull a timeline, and answer from it.
Naming just the automation is enough. Its runs, its variables, and the
entities its config refers to are all included. You can follow up in the same
chat. For example, ask it to widen the window to
`6h` and it will pull again.

**Small models:** tool calling is where small local models struggle most.
- If the model never calls the tool, set the model's Function Calling to
  **Native** (Advanced Params) if your runtime supports it, or try a larger
  model.
- You can also skip the search step by giving the exact IDs:
  "Get the timeline for automation.x and climate.y over the last 2h."

**It can't create Home Assistant automations.** If you ask for a new or
changed automation, a good model writes the YAML in its reply for you to paste
into Home Assistant. A model may instead grab another tool that has
"automation" in its name, such as Open WebUI's own scheduled automations,
which run chat prompts on a timer and have nothing to do with Home Assistant.
Then it may tell you it created a Home Assistant automation when it didn't.
To avoid that:

- Turn off Open WebUI's automation or scheduling tools for the model you use
  with this tool.
- Ask for YAML explicitly: "Write the Home Assistant automation YAML for ...".
- Check the result in Home Assistant itself, not in the chat.

## Data and safety

- The tool only reads from Home Assistant. It never calls services or writes
  state.
- The token is stored in Open WebUI's database as a valve, not in the tool
  file. Valves are admin-only.
- With `keep_raw` on (the default), each pull is saved under `out_dir`
  (`/app/backend/data/ha-llm-context`, in Open WebUI's data volume). You can
  condense it again later from a shell. Turn `keep_raw` off to keep nothing.

## Updating

The `requirements:` line is installed once. To pick up new code:
1. Bump `version` in `pyproject.toml` and push.
2. Reinstall and restart. `open-webui` is the usual container name; check
   yours with `docker ps`:

   ```sh
   docker exec open-webui pip install --force-reinstall --no-deps \
     "https://github.com/mikepell007/ha-llm-context/archive/refs/heads/main.zip"
   docker restart open-webui
   ```

   That URL is the one in the tool file's `requirements:` line. It installs
   whatever is on `main` on GitHub, so push before you reinstall.
3. If the tool file itself changed (new valves or parameters), paste it into
   Open WebUI again too.
