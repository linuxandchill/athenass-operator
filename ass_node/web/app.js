const sessionToken = document.querySelector(
    'meta[name="athenass-session"]',
).content;
let state = null;
let loginTimer = null;
let logCursor = 0;
let stopPending = false;
let nodesRefreshing = false;

const element = (id) => {
    const found = document.getElementById(id);
    if (!found) throw new Error(`Missing element #${id}`);
    return found;
};

async function api(path, options = {}) {
    const headers = {
        "X-AthenaSS-Session": sessionToken,
        ...(options.headers || {}),
    };
    if (options.body) headers["Content-Type"] = "application/json";
    const response = await fetch(path, { ...options, headers });
    const result = await response.json();
    if (!response.ok) {
        const error = new Error(
            result.error ||
                result.detail ||
                `Request failed (${response.status})`,
        );
        error.code = result.code;
        throw error;
    }
    return result;
}

function showNotice(message, kind = "ok") {
    const notice = element("notice");
    notice.textContent = message;
    notice.className = `notice ${kind}`;
    window.setTimeout(() => notice.classList.add("hidden"), 5000);
}

function setRunState(running, label = running ? "Online" : "Idle") {
    element("start-button").disabled =
        running || !state?.configured || !state?.authenticated;
    element("stop-button").disabled = !running || stopPending;
    const status = element("node-status");
    const indicator = document.createElement("i");
    status.className = `status ${running ? "running" : "idle"}`;
    status.replaceChildren(indicator, document.createTextNode(` ${label}`));
}

function renderState() {
    const authenticated = Boolean(state?.authenticated);
    const badge = element("session-badge");
    badge.textContent = authenticated ? "Signed in" : "Not signed in";
    badge.className = `badge ${authenticated ? "authenticated" : ""}`;
    const accountButton = element("login-button");
    accountButton.disabled = false;
    accountButton.textContent = authenticated ? "Sign out" : "Sign in";
    const running = Boolean(state?.runner?.running);
    const saveButton = element("register-button");
    saveButton.disabled = !authenticated || running;
    saveButton.textContent = state?.node
        ? "Save node changes"
        : "Register node";
    setRunState(running);

    const configured = element("configured-node");
    if (!state?.node) {
        configured.className = "configured muted";
        configured.textContent = "No node configured locally.";
        return;
    }

    configured.className = "configured";
    const name = document.createElement("strong");
    const model = document.createElement("span");
    const endpoint = document.createElement("span");
    const publicEndpoint = document.createElement("span");
    name.textContent = state.node.name || "Unnamed node";
    model.textContent = state.node.model_id || "Unknown model";
    endpoint.textContent = state.node.endpoint || "No local endpoint";
    publicEndpoint.textContent =
        state.node.public_endpoint || "Connectivity not provisioned";
    configured.replaceChildren(name, model, endpoint, publicEndpoint);

    element("node-name").value = state.node.name || "";
    element("model-id").value = state.node.model_id || "";
    element("command").value = state.node.command || "";
    const machine = state.node.machine_info || {};
    element("machine-details").value = JSON.stringify(machine, null, 2);
    try {
        const port = new URL(state.node.endpoint).port;
        if (port) element("port").value = port;
    } catch (_error) {
        // Leave the editable port unchanged when old local configuration is malformed.
    }
}

async function refreshState() {
    state = await api("/api/state");
    renderState();
    if (state.authenticated) await refreshNodes();
}

async function beginLogin() {
    const button = element("login-button");
    button.disabled = true;
    button.textContent = "Opening browser…";
    try {
        const auth = await api("/api/login/start", { method: "POST" });
        if (typeof auth.verification_url !== "string" || !auth.verification_url) {
            throw new Error("AthenaSS did not provide an authorization URL.");
        }
        const code = element("login-code");
        const strong = document.createElement("strong");
        const link = document.createElement("a");
        const lineBreak = document.createElement("br");
        strong.textContent = auth.user_code;
        link.href = auth.verification_url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.textContent = auth.verification_url;
        code.classList.remove("hidden");
        code.replaceChildren(
            document.createTextNode("Enter code "),
            strong,
            document.createTextNode(" at:"),
            lineBreak,
            link,
        );
        const interval = Math.max(auth.interval || 5, 2) * 1000;
        loginTimer = window.setInterval(async () => {
            try {
                const result = await api("/api/login/poll", {
                    method: "POST",
                    body: JSON.stringify({ device_code: auth.device_code }),
                });
                if (result.status === "pending") return;
                window.clearInterval(loginTimer);
                loginTimer = null;
                if (result.status !== "approved")
                    throw new Error(result.error || "Authorization failed");
                code.classList.add("hidden");
                showNotice("Signed in to AthenaSS.");
                await refreshState();
            } catch (error) {
                window.clearInterval(loginTimer);
                loginTimer = null;
                button.disabled = false;
                button.textContent = "Sign in";
                showNotice(error.message || String(error), "error");
            }
        }, interval);
    } catch (error) {
        button.disabled = false;
        button.textContent = "Sign in";
        showNotice(error.message || String(error), "error");
    }
}

async function signOut() {
    const button = element("login-button");
    button.disabled = true;
    button.textContent = "Signing out…";
    try {
        const runner = state?.runner || { running: false };
        state = await api("/api/logout", { method: "POST" });
        state.runner = runner;
        renderState();
        await refreshNodes();
        showNotice("Signed out. Local node configuration was kept.");
    } catch (error) {
        renderState();
        showNotice(error.message || String(error), "error");
    }
}

function handleAccountAction() {
    if (state?.authenticated) {
        signOut();
    } else {
        beginLogin();
    }
}

async function registerNode(event) {
    event.preventDefault();
    const button = element("register-button");
    const editing = Boolean(state?.node?.id);
    const runner = state?.runner || { running: false };
    button.disabled = true;
    button.textContent = editing ? "Saving…" : "Registering…";
    const machineDetails = element("machine-details").value;
    try {
        state = await api(
            editing ? "/api/nodes/update" : "/api/nodes/register",
            {
                method: "POST",
                body: JSON.stringify({
                    ...(editing ? { node_id: state.node.id } : {}),
                    name: element("node-name").value,
                    model_id: element("model-id").value,
                    command: element("command").value,
                    port: Number(element("port").value),
                    machine_info_json: machineDetails,
                }),
            },
        );
        state.runner = runner;
        renderState();
        await refreshNodes();
        showNotice(
            editing
                ? "Node details updated."
                : "Node registered and connectivity provisioned.",
        );
    } catch (error) {
        showNotice(error.message || String(error), "error");
    } finally {
        renderState();
    }
}

async function refreshNodes() {
    if (nodesRefreshing) return;
    nodesRefreshing = true;
    const list = element("node-list");
    if (!state?.authenticated) {
        list.textContent = "Sign in to load registered nodes.";
        nodesRefreshing = false;
        return;
    }
    try {
        const result = await api("/api/nodes");
        if (!result.nodes.length) {
            list.className = "node-list muted";
            list.textContent = "No nodes registered.";
            return;
        }
        list.className = "node-list";
        const rows = result.nodes.map((node) => {
            const row = document.createElement("div");
            const name = document.createElement("strong");
            const model = document.createElement("span");
            const status = document.createElement("span");
            const deleteButton = document.createElement("button");
            row.className = "node-row";
            name.textContent = node.name || "Unnamed";
            model.textContent = node.model_id || "No model";
            status.className = "node-state";
            status.textContent = {
                healthy: "Healthy",
                in_use: "In Use",
                offline: "Offline",
            }[node.effective_status] || (
                node.status === "offline" ? "Offline" : "Status unknown"
            );
            deleteButton.type = "button";
            deleteButton.className = "delete-node";
            deleteButton.textContent = "Delete";
            deleteButton.addEventListener("click", () =>
                deleteNode(node.name, deleteButton),
            );
            row.replaceChildren(name, model, status, deleteButton);
            return row;
        });
        list.replaceChildren(...rows);
    } catch (error) {
        list.className = "node-list error-text";
        list.textContent = error.message || String(error);
    } finally {
        nodesRefreshing = false;
    }
}

async function deleteNode(name, button) {
    const confirmed = window.confirm(
        `Delete node "${name}"? It will disappear from node lists. Historical usage records are kept.`,
    );
    if (!confirmed) return;

    button.disabled = true;
    button.textContent = "Deleting…";
    try {
        await api("/api/nodes/delete", {
            method: "POST",
            body: JSON.stringify({ name }),
        });
        await refreshState();
        showNotice(`Node "${name}" deleted.`);
    } catch (error) {
        button.disabled = false;
        button.textContent = "Delete";
        showNotice(error.message || String(error), "error");
    }
}

async function startNode() {
    try {
        element("logs").textContent = "Starting node…\n";
        logCursor = 0;
        const runner = await api("/api/node/start", { method: "POST" });
        state.runner = runner;
        setRunState(true, "Starting");
    } catch (error) {
        showNotice(error.message || String(error), "error");
    }
}

async function stopNode() {
    if (stopPending) return;
    stopPending = true;
    element("stop-button").disabled = true;
    try {
        let runner;
        try {
            runner = await api("/api/node/stop", {
                method: "POST",
                body: JSON.stringify({ force: false }),
            });
        } catch (error) {
            if (!["node_in_use", "reservation_unknown"].includes(error.code))
                throw error;
            const dialog = element("stop-warning");
            element("stop-warning-text").textContent = error.message;
            dialog.returnValue = "cancel";
            const confirmed = new Promise((resolve) =>
                dialog.addEventListener("close", () =>
                    resolve(dialog.returnValue === "stop"), { once: true }),
            );
            dialog.showModal();
            element("keep-running").focus();
            if (!(await confirmed)) return;
            runner = await api("/api/node/stop", {
                method: "POST",
                body: JSON.stringify({ force: true }),
            });
        }
        state.runner = runner;
        await refreshNodes();
    } catch (error) {
        showNotice(error.message || String(error), "error");
    } finally {
        stopPending = false;
        setRunState(Boolean(state?.runner?.running));
    }
}

async function refreshLogs() {
    try {
        const result = await api(`/api/node/logs?after=${logCursor}`);
        if (result.lines.length) {
            const output = element("logs");
            if (logCursor === 0) output.textContent = "";
            for (const entry of result.lines)
                output.textContent += `${entry.line}\n`;
            output.scrollTop = output.scrollHeight;
            logCursor = result.cursor;
        }
        if (state) {
            state.runner = result;
            setRunState(result.running, result.running ? "Online" : "Idle");
        }
    } catch (_error) {
        // A temporary failed poll should not interrupt the operator controls.
    }
}

window.addEventListener("DOMContentLoaded", () => {
    element("login-button").addEventListener("click", handleAccountAction);
    element("node-form").addEventListener("submit", registerNode);
    element("start-button").addEventListener("click", startNode);
    element("stop-button").addEventListener("click", stopNode);
    element("refresh-button").addEventListener("click", refreshNodes);
    refreshState().catch((error) =>
        showNotice(error.message || String(error), "error"),
    );
    window.setInterval(refreshLogs, 1000);
    window.setInterval(refreshNodes, 10000);
});
