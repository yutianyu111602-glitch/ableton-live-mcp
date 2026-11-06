/* Node for Max transport. No external dependencies; max-api is provided by Max. */
"use strict";
const net = require("net");
const crypto = require("crypto");
const fs = require("fs");
const path = require("path");
const MAX_BYTES = 1024 * 1024;

function createServer(options) {
    const sendToMax = options.sendToMax;
    const timeoutMs = options.timeoutMs || 15000;
    const maxConnections = options.maxConnections || 8;
    const pending = new Map();
    const sockets = new Set();
    const ownerCounts = new Map();
    let shuttingDown = false;
    let catalogue = options.schema || null;
    if (!catalogue) {
        try { catalogue = JSON.parse(fs.readFileSync(path.join(__dirname, "lom_schema.json"), "utf8")); }
        catch (error) { catalogue = null; } // Unknown effects remain conservative.
    }
    function mayWrite(request) {
        if (request.dry_run !== false) return false;
        if (request.op === "set") return true;
        if (request.op !== "call") return false;
        let type = request.object_type;
        const seen = new Set();
        while (catalogue && catalogue.objects && Object.hasOwn(catalogue.objects, type) && !seen.has(type)) {
            seen.add(type);
            const definition = catalogue.objects[type];
            if (Object.hasOwn(definition.members || {}, request.member)) {
                const member = definition.members[request.member];
                return member.kind !== "functions" || member.mutating !== false;
            }
            type = definition.parent;
        }
        return true;
    }
    function reply(socket, id, result) {
        if (!socket.destroyed) socket.write(JSON.stringify({id, result}) + "\n");
    }
    function dispatch(request, socket, clientId) {
        if (pending.size >= 64) {
            if (socket) reply(socket, clientId, {status: "error", code: "request_limit", write_may_have_occurred: false});
            return;
        }
        const transportId = crypto.randomBytes(16).toString("hex");
        const timer = setTimeout(() => {
            pending.delete(transportId);
            if (socket) reply(socket, clientId, {status: "partial", code: "max_request_timeout",
                write_may_have_occurred: mayWrite(request)});
        }, timeoutMs);
        timer.unref();
        pending.set(transportId, {socket, clientId, timer, mayWrite: mayWrite(request)});
        function dispatchFailed() {
            if (!pending.has(transportId)) return;
            clearTimeout(timer); pending.delete(transportId);
            if (socket) reply(socket, clientId, {status: "partial", code: "max_dispatch_failed",
                write_may_have_occurred: mayWrite(request)});
        }
        try {
            const delivery = sendToMax(transportId, JSON.stringify(request));
            if (delivery && typeof delivery.catch === "function") delivery.catch(dispatchFailed);
        }
        catch (error) {
            dispatchFailed();
        }
    }
    const server = net.createServer(socket => {
        if (shuttingDown || sockets.size >= maxConnections) { socket.destroy(); return; }
        sockets.add(socket);
        socket.setNoDelay(true);
        socket.setTimeout(600000, () => socket.destroy());
        socket._lomOwners = new Set();
        let buffer = Buffer.alloc(0);
        socket.on("data", chunk => {
            if (chunk.length + buffer.length > MAX_BYTES + 1) { socket.destroy(); return; }
            buffer = Buffer.concat([buffer, chunk]);
            let newline;
            while ((newline = buffer.indexOf(10)) >= 0) {
                const frameBytes = buffer.subarray(0, newline);
                buffer = buffer.subarray(newline + 1);
                let frame;
                try {
                    if (frameBytes.length > MAX_BYTES) throw new Error("frame_too_large");
                    frame = JSON.parse(frameBytes.toString("utf8"));
                    if (!frame || typeof frame.id !== "string" || !frame.id.length || frame.id.length > 128 ||
                        !frame.request || typeof frame.request !== "object" || Array.isArray(frame.request)) throw new Error("invalid_frame");
                    const owner = frame.request.owner;
                    if (owner !== undefined) {
                        if (typeof owner !== "string" || !/^[A-Za-z0-9:_-]{1,128}$/.test(owner)) throw new Error("invalid_owner");
                        if (!socket._lomOwners.has(owner)) {
                            if (socket._lomOwners.size >= 8) throw new Error("owner_limit");
                            socket._lomOwners.add(owner);
                            ownerCounts.set(owner, (ownerCounts.get(owner) || 0) + 1);
                        }
                    }
                    dispatch(frame.request, socket, frame.id);
                } catch (error) {
                    reply(socket, frame && typeof frame.id === "string" ? frame.id : null,
                        {status: "error", code: "invalid_frame", write_may_have_occurred: false});
                }
            }
        });
        socket.on("error", () => {});
        socket.on("close", () => {
            sockets.delete(socket);
            for (const owner of socket._lomOwners) {
                const remaining = (ownerCounts.get(owner) || 1) - 1;
                if (remaining > 0) ownerCounts.set(owner, remaining);
                else { ownerCounts.delete(owner); dispatch({op: "cleanup", owner}, null, null); }
            }
            for (const [id, item] of pending) {
                if (item.socket === socket) { clearTimeout(item.timer); pending.delete(id); }
            }
        });
    });
    return {
        server,
        listen(port = 9878) {
            if (!Number.isInteger(port) || port < 0 || port > 65535) throw new Error("Invalid loopback port");
            return new Promise((resolve, reject) => {
                server.once("error", reject);
                server.listen(port, "127.0.0.1", () => { server.removeListener("error", reject); resolve(server.address()); });
            });
        },
        response(id, json) {
            const item = pending.get(id);
            if (!item) return; // Timed-out writes are never retried.
            pending.delete(id); clearTimeout(item.timer);
            let result;
            try {
                if (Buffer.byteLength(json, "utf8") > 4 * MAX_BYTES) throw new Error("oversize");
                result = JSON.parse(json);
            } catch (error) { result = {status: "partial", code: "invalid_max_response", write_may_have_occurred: item.mayWrite}; }
            if (item.socket) reply(item.socket, item.clientId, result);
        },
        close() {
            shuttingDown = true;
            for (const socket of sockets) socket.destroy();
            for (const item of pending.values()) clearTimeout(item.timer);
            pending.clear();
            return new Promise(resolve => server.close(() => resolve()));
        }
    };
}

module.exports = {createServer, MAX_BYTES};
let maxApi = null;
try { maxApi = require("max-api"); } catch (error) {
    if (require.main === module) throw new Error("This script is hosted by Node for Max; max-api is provided by Max.");
}
if (maxApi) {
    const bridge = createServer({sendToMax: (id, request) => maxApi.outlet("request", id, request)});
    maxApi.addHandler("response", (id, result) => bridge.response(String(id), String(result)));
    maxApi.addHandler("bridge_ready", () => maxApi.post("LOM API bridge ready (loopback only)."));
    maxApi.addHandler("bridge_error", detail => maxApi.post("LOM API bridge is not ready: " + String(detail)));
    maxApi.addHandler("bridge_state", () => {}); // Detailed bootstrap state is available through request op=readiness.
    maxApi.addHandler("shutdown", () => bridge.close());
    const port = process.env.ABLETON_MAX_PORT === undefined ? 9878 : Number(process.env.ABLETON_MAX_PORT);
    bridge.listen(port).then(() => maxApi.outlet("request_readiness"))
        .catch(error => maxApi.post("LOM API listener failed: " + String(error)));
    process.once("SIGTERM", () => bridge.close());
    process.once("SIGINT", () => bridge.close());
}
