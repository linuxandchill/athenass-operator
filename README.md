# AthenaSS Operator (A77)

AthenaSS Operator (A77) is a local web application for registering and running a GPU inference server on the AthenaSS (A77) network. It works on macOS, Linux, and Windows.

## Install and run

Users only need to install `uv`, then run AthenaSS Operator directly from GitHub. They do not need to clone this repository, create a virtual environment, or install Python packages manually.

### 1. Install `uv`

**macOS or Linux**

Open Terminal and run:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**Windows**

Open PowerShell and run:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Close and reopen the terminal if the installer asks you to refresh your shell. Confirm the installation:

```bash
uv --version
```

See the [official uv installation guide](https://docs.astral.sh/uv/getting-started/installation/) for alternative installation methods.

### 2. Start AthenaSS Operator

Run this single command:

```bash
uvx --from git+https://github.com/linuxandchill/athenass-operator athenass-operator
```

The first run downloads AthenaSS Operator, its runtime, and its dependencies into an isolated cache. The browser interface then opens automatically at:

```text
http://127.0.0.1:8930
```

Keep the terminal window open while operating an Endpoint. Closing the browser tab does not stop the service; revisit the URL to reopen it. Press `Ctrl+C` in the terminal to stop AthenaSS Operator.

Use the same `uvx` command whenever you want to run AthenaSS Operator again. Later starts use uv's cache and are faster.

### Optional: install a permanent command

Instead of using the longer `uvx` command each time:

```bash
uv tool install git+https://github.com/linuxandchill/athenass-operator
athenass-operator
```

If `athenass-operator` is not found after installation, run `uv tool update-shell`, reopen the terminal, and try again.

### What users install separately

AthenaSS Operator does not install an inference engine, models, GPU drivers, or CUDA. Install and test a compatible inference server before using the Operator.

### What AthenaSS Operator provides

- AthenaSS sign-in
- Endpoint registration and management
- Start and stop controls
- Live inference logs: the interface shows the latest 200 lines; the operator keeps a rolling 2,000-line buffer in memory, not on disk or in Supabase. The buffer clears when a new Endpoint worker starts or the operator exits. Your inference engine may maintain its own separate log files.
- Registered-Endpoint listing

Engine adapters are intentionally unnecessary: AthenaSS Operator runs the command exactly as entered and verifies the common `GET /v1/models` contract. The command runs under your local user account, so only enter commands you trust. The command is also public Endpoint metadata; never include API keys, tokens, passwords, or other secrets.

The control server binds strictly to `127.0.0.1`. Its state-changing API requires a random per-process session header injected into the same-origin page, preventing unrelated websites from operating the Endpoint through localhost.

No separate connectivity software installation or third-party account configuration is required. AthenaSS (A77) provisions a restricted connectivity credential during Endpoint registration.

## Use the interface

Everything needed for normal operation is available in the browser:

1. Select **Sign in** and approve the AthenaSS login in the browser.
2. Enter an Endpoint name, model ID, local inference port, and launch command.
3. Enter **Machine details (JSON)** as a valid JSON object using any useful fields, such as `gpu`, `cpu`, `memory`, `operating_system`, `engine`, `quantization`, or `context`.
4. Select **Register Endpoint** the first time. After configuration, edit the same form and select **Save Endpoint changes** to update the existing Endpoint—including its name and machine details—without creating a duplicate.
5. Select **Start Endpoint**. The operator launches the inference command and displays its logs.
6. Wait for the status to become online.
7. Select **Stop** when the Endpoint should go offline. Use **Delete** in the registered-Endpoint list to remove an Endpoint from active listings; historical usage records are retained.

The separate command interface is an internal implementation detail. Operators do not need to invoke it.

### Endpoint health and reservations

The registered-Endpoint list refreshes every 10 seconds:

- **Healthy**: the Endpoint has a recent online heartbeat and no active reservation.
- **In Use**: the Endpoint is healthy and has an active, unexpired reservation.
- **Offline**: the Endpoint is offline or its heartbeat is stale.

**Stop** checks the current reservation state before shutting down. If the Endpoint is reserved, a warning offers **Keep running** (the default) or **Stop anyway**. Stopping anyway interrupts the renter's reservation and requests. If reservation status cannot be checked, the Operator warns instead of assuming the Endpoint is free.

The warning is a point-in-time check, not an atomic block on new reservations. Ctrl+C and deletion are separate shutdown paths and do not use this Stop confirmation.

This feature requires the updated AthenaSS API `GET /v1/cli/nodes` response (`effective_status` and `reserved`). With an older API, online entries display **Status unknown** and Stop requires confirmation. The API restricts the lookup to the authenticated user's Endpoints and returns no renter details. No database credentials, schema changes, or RLS grants are needed in the Operator.

Machine details are required, seller-reported JSON metadata. Renters see each top-level field separately; the values are not automatically detected or independently verified.

## Local state

AthenaSS Operator stores local state under `${XDG_CONFIG_HOME:-~/.config}/ass-node/`:

| File               | Contents                                                                                      |
| ------------------ | --------------------------------------------------------------------------------------------- |
| `credentials.json` | User ID and account-level access token created during sign-in.                                |
| `config.json`      | Launch command, endpoint, model metadata, Endpoint token, and managed connectivity configuration. |
| `serve.pid`        | PID used for local shutdown coordination.                                                     |

Credential and configuration files use owner-only permissions (`0600`) on Unix-like systems. Treat `credentials.json` and `config.json` as secrets; never commit or share them.

## Environment variables

| Variable           | Purpose                                                       | Default                                  |
| ------------------ | ------------------------------------------------------------- | ---------------------------------------- |
| `ATHENASS_API_URL` | Override the AthenaSS (A77) control-plane API base URL.        | `https://api.athenass.com`               |
| `ASS_CONFIG_DIR`   | Override the complete local state directory.                  | `${XDG_CONFIG_HOME:-~/.config}/ass-node` |
| `XDG_CONFIG_HOME`  | Change the parent state directory when the override is unset. | `~/.config`                              |


