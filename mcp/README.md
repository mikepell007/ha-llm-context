# MCP server

`ha-llm-context mcp` serves the same two read-only tools as the
[Open WebUI tool](../openwebui/README.md) over the Model Context Protocol, so
any MCP client can use them:

- **find_home_assistant_entities**: looks up entity IDs from words like
  "office climate".
- **get_home_assistant_timeline**: pulls and condenses a time window. It
  returns the same timeline the CLI writes to `timeline.txt`.

Both tools are marked read-only. They never call services or write state.

## Two ways to run it

| | Started by the client (stdio) | Long-running service (HTTP) |
|---|---|---|
| Good for | LM Studio and other desktop apps on the same machine | Open WebUI, or several clients sharing one server |
| Runs | only while the client is open | all the time, as a container or systemd service |
| Settings | the client's `env` block | the container's or service's environment |
| Address | none | `http://<host>:8765/mcp` |

Both read `HA_URL` and `HA_TOKEN`. The model connection belongs to the client,
so the `LLM_*` settings aren't used.

### As a service in Docker (for Open WebUI)

The image builds straight from GitHub, so you only need two files on the PC
that runs Open WebUI. Copy [compose.yaml](compose.yaml) into a folder, and put
a `.env` next to it:

```sh
HA_URL=http://192.168.1.10:8123
HA_TOKEN=<long-lived token from an admin user>
MCP_TOKEN=<any long random string, e.g. from: openssl rand -hex 32>
HA_TIMEZONE=America/New_York
```

Then:

```sh
docker compose up -d --build
docker logs ha-llm-context      # "MCP server on http://0.0.0.0:8765/mcp"
```

To update after a push: `docker compose up -d --build` again.

**MCP_TOKEN matters.** The server answers with your home's history and
presence, and the compose file publishes port 8765 on your network. With
`MCP_TOKEN` set, every request needs `Authorization: Bearer <MCP_TOKEN>`, and
anything without it gets 401. If you'd rather not publish the port at all,
remove `ports:` and attach the container to Open WebUI's Docker network. Open
WebUI can then reach it at `http://ha-llm-context:8765/mcp`.

**Connect Open WebUI.** Recent versions connect to MCP servers directly: Admin
Panel → Settings → External Tools → **+**, then choose type **MCP (Streamable
HTTP)**. Set:
- URL: `http://<PC's IP>:8765/mcp`, or `http://ha-llm-context:8765/mcp` on a
  shared Docker network. `localhost` there means Open WebUI's own container,
  not the PC.
- Auth: Bearer, with your `MCP_TOKEN`.

Then turn the tools on for a model, as with any tool. Older versions without
MCP support need the `mcpo` proxy in between.

If you also have the pasted Open WebUI tool, turn one of them off for the
model. Otherwise it sees every tool twice.

### Without Docker (systemd)

```sh
python3 -m venv ~/ha-llm-context && ~/ha-llm-context/bin/pip install \
  "ha-llm-context[mcp] @ https://github.com/mikepell007/ha-llm-context/archive/refs/heads/main.zip"
```

`~/.config/systemd/user/ha-llm-context.service`:

```ini
[Unit]
Description=ha-llm-context MCP server

[Service]
EnvironmentFile=%h/.config/ha-llm-context.env
ExecStart=%h/ha-llm-context/bin/ha-llm-context mcp --http --host 0.0.0.0 --tz America/New_York
Restart=on-failure

[Install]
WantedBy=default.target
```

Put `HA_URL`, `HA_TOKEN` and `MCP_TOKEN` in `~/.config/ha-llm-context.env`
(`chmod 600` it), then run `systemctl --user enable --now ha-llm-context`. To
start it at boot without logging in, also run `loginctl enable-linger`.

### Started by the client (LM Studio and similar)

Install it as above, without the service. Then add it to the client's MCP
config. In LM Studio, that's Program → Install → Edit `mcp.json`:

```json
{
  "mcpServers": {
    "home-assistant-timeline": {
      "command": "/home/you/ha-llm-context/bin/ha-llm-context",
      "args": ["mcp", "--tz", "America/New_York"],
      "env": { "HA_URL": "http://192.168.1.10:8123", "HA_TOKEN": "<token>" }
    }
  }
}
```

The client starts the server when it needs it and stops it afterwards. No port
is opened, so MCP_TOKEN isn't needed.

## Options

`ha-llm-context mcp --help` lists them all. The main ones:

- `--http`, `--host`, `--port`: serve HTTP instead of stdio. The defaults are
  127.0.0.1 and port 8765; use 0.0.0.0 in a container.
- `--tz`: display time zone. Without it, times show in the server's own zone,
  which is UTC in a container.
- `--keep-raw DIR`: keep each raw pull and its timeline, so you can condense
  it again with the CLI. By default nothing is kept.
- `--no-related`, `--max-related`, `--max-changes`, `--max-errors`,
  `--no-config`, `--attr`: the same as the CLI's options.

## Privacy

The server sends your Home Assistant data to whichever model the client uses.
Keep that model local. That's the point of this project.
