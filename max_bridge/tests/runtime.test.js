"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const net = require("node:net");
const vm = require("node:vm");
const {createLomBridge} = require("../max_lom_runtime.js");
const {createServer, MAX_BYTES} = require("../node_bridge.js");
const schema = JSON.parse(fs.readFileSync(path.resolve(__dirname, "../../ableton_live_mcp/lom_schema.json"), "utf8"));

function bootstrapFixture(initialFiles) {
    const files = {...initialFiles};
    const outputs = [];
    const reads = [];
    let nativeCreates = 0;
    function File(filename) {
        reads.push(filename);
        const source = files[filename];
        this.isopen = source !== undefined;
        this.position = 0;
        this.eof = source ? source.length : 0;
        this.readstring = count => {
            const value = source.slice(this.position, this.position + count);
            this.position += value.length;
            return value;
        };
        this.close = () => { this.isopen = false; };
    }
    const context = {File, patcher: {filepath: "/device-folder/LOM_API_Bridge.amxd"},
        LiveAPI: function () { nativeCreates++; return {id: 1, valid: 1, type: "Song", info: "", mode: 0}; },
        outlet: (...args) => outputs.push(args), console};
    vm.runInNewContext(fs.readFileSync(path.resolve(__dirname, "../max_lom_runtime.js"), "utf8"), context);
    return {context, files, outputs, reads, nativeCreates: () => nativeCreates,
        reply: () => JSON.parse(outputs.filter(row => row[1] === "response").at(-1)[3])};
}

function declared(type) {
    const members = {};
    const seen = new Set();
    while (type && !seen.has(type)) {
        seen.add(type);
        const definition = schema.objects[type];
        for (const [name, item] of Object.entries(definition.members)) if (!members[name]) members[name] = item;
        type = definition.parent;
    }
    return members;
}
function sample(spec, registry) {
    if (!spec) return 0;
    if (spec.oneOf) return sample(spec.oneOf[0], registry);
    if (spec.enum) return spec.enum[0];
    if (spec.dictionary) return sample(spec.dictionary, registry);
    const type = spec.value_type || spec.type;
    if (type === "reference" || spec.$ref_types) {
        const ref = registry.byType[(spec.ref_types || spec.$ref_types || [])[0]] || registry.byType.Track;
        return {$live_id: ref.id, identity: "max:test:" + ref.id, object_type: ref.type};
    }
    if (["int", "integer", "float", "number"].includes(type)) {
        return typeof spec.minimum === "number" ? spec.minimum : typeof spec.exclusiveMinimum === "number" ? spec.exclusiveMinimum + 1 : 0;
    }
    if (["bool", "boolean"].includes(type)) return false;
    if (["symbol", "string"].includes(type)) return "value";
    if (["list", "array"].includes(type)) return [];
    if (["dictionary", "object"].includes(type)) {
        const result = {};
        for (const key of spec.required || []) result[key] = sample(spec.properties[key], registry);
        return result;
    }
    return 0;
}
class FakeDict {
    parse(raw) { this.data = JSON.parse(raw); }
    stringify() { return JSON.stringify(this.data); }
    freepeer() { this.freed = true; }
}
function fixture() {
    const registry = {byId: {}, byType: {}, byPath: {}, writes: [], reads: [], observed: [], calls: [], observers: [], dictionaries: [], apiPaths: []};
    let nextId = 1;
    let milliseconds = 1700000000000;
    for (const type of Object.keys(schema.objects)) {
        if (type === "this_device") continue;
        const object = {id: nextId++, type, values: {}, members: declared(type)};
        registry.byId[object.id] = object;
        registry.byType[type] = object;
    }
    registry.byType.this_device = registry.byType.MaxDevice;
    Object.assign(registry.byPath, {live_set: registry.byType.Song, live_app: registry.byType.Application,
        this_device: registry.byType.MaxDevice, "live_set tracks 0": registry.byType.Track,
        "live_set tracks 0 devices 0": registry.byType.PluginDevice,
        "live_set tracks 0 devices 0 parameters 0": registry.byType.DeviceParameter,
        "live_set tracks 0 arrangement_clips 0": registry.byType.Clip});
    Object.assign(registry.byPath, {"live_set view": registry.byType["Song.View"],
        "live_app view": registry.byType["Application.View"], "live_set tracks 0 view": registry.byType["Track.View"],
        "live_set tracks 0 devices 0 view": registry.byType["Device.View"],
        "live_set tracks 0 devices 1": registry.byType.Eq8Device,
        "live_set tracks 0 devices 1 view": registry.byType["Eq8Device.View"]});
    for (const [canonical, object] of Object.entries(registry.byPath)) object.canonicalPath = canonical;
    for (const object of Object.values(registry.byId)) {
        for (const [name, spec] of Object.entries(object.members)) if (spec.kind !== "functions") object.values[name] = sample(spec, registry);
    }
    registry.byType.DeviceParameter.values.min = 0;
    registry.byType.DeviceParameter.values.max = 1;
    for (const object of Object.values(registry.byId)) {
        const view = object.members.view;
        if (!view) continue;
        const candidate = registry.byType[object.type + ".View"] || registry.byType[view.ref_types[0]];
        object.values.view = ["id", candidate.id];
    }
    function api(callback, targetPath) {
        registry.apiPaths.push(targetPath);
        const object = targetPath.startsWith("id ") ? registry.byId[Number(targetPath.slice(3))] : registry.byPath[targetPath];
        let id = object ? object.id : 0;
        let property = "";
        const instance = {
            get id() { return registry.legacyValidZero ? String(id) : id; }, set id(value) { id = value; callback(["id", value]); },
            get valid() { return registry.legacyValidZero ? 0 : id ? 1 : 0; },
            get type() { return object ? object.rawType || (registry.genericViews && object.type.endsWith(".View") ? "View" : object.type) : ""; },
            get path() { return registry.canonicalPaths && object ? object.canonicalPath || targetPath : undefined; },
            get unquotedpath() { return registry.canonicalPaths && object ? object.canonicalPath || targetPath : undefined; },
            get children() { return object ? Object.values(object.members).filter(i => i.kind === "children").map(i => i.name) : []; },
            get info() { return object ? object.infoOverride !== undefined ? object.infoOverride : "id " + id + "\ntype " + instance.type + "\n" + Object.values(object.members).map(i => (i.kind === "functions" ? "function" : i.kind === "children" ? "child" : "property") + " " + i.name + " type").join("\n") : ""; },
            get property() { return property; },
            set property(value) { if (!value && object.failDetach) throw new Error("detach failed"); property = value; if (value) { registry.observed.push({object, name: value}); callback([value, object.values[value]]); } },
            get(name) { registry.reads.push({object, name}); if (object.failGet === name) throw new Error("getter failed"); return Array.isArray(object.values[name]) ? object.values[name] : [object.values[name]]; },
            set(name, ...args) {
                let value;
                const descriptor = object.members[name];
                if (descriptor.value_type === "reference") {
                    assert.equal(args.length, 2, "LiveAPI reference setter needs property, id atom and native ID as three arguments");
                    assert.equal(args[0], "id", "A single id-tag string or array is not the reference setter ABI");
                    assert.equal(typeof args[1], "number");
                    assert.ok(Number.isInteger(args[1]) && args[1] >= -2147483648 && args[1] <= 2147483647);
                    assert.ok(args[1] !== 0 || descriptor.nullable === true);
                    value = ["id", args[1]]; // Emulate the native getter's response shape, not its setter ABI.
                } else {
                    assert.equal(args.length, 1, "Scalar/list/dictionary setters retain their two-argument ABI");
                    value = args[0];
                }
                registry.writes.push({object, name, value, arguments: [name, ...args]});
                if (object.ignoreSet !== name) object.values[name] = object.float32Field === name ? Math.fround(value) : object.clampField === name ? 0.7 : value;
                if (object.failSet === name) throw new Error("setter failed after changing"); },
            call(name, ...args) {
                for (const [index, descriptor] of (object.members[name].arguments || []).entries()) {
                    if (descriptor.value_type === "reference" && index < args.length) {
                        assert.equal(typeof args[index], "string", "Max Live object arguments are tagged atoms, not bare integers");
                        assert.match(args[index], descriptor.nullable ? /^id (?:0|-?[1-9][0-9]*)$/ : /^id -?[1-9][0-9]*$/);
                    }
                }
                registry.calls.push({object, name, args});
                if (object.failCall === name) throw new Error("native failure");
                return null;
            },
            notify(value) { callback([property, value]); }
        };
        registry.observers.push(instance);
        callback(["id", id]);
        return instance;
    }
    const bridge = createLomBridge(schema, {scope: "test", now: () => milliseconds, makeAPI: api,
        makeDict: () => { const d = new FakeDict(); registry.dictionaries.push(d); return d; }});
    return {bridge, registry, advance: ms => milliseconds += ms};
}
function request(type, op, member, extra, f) {
    const object = f.registry.byType[type];
    const payload = Object.assign({op, object_type: type, path: "id " + object.id,
        owner: "test-owner", expected_identity: "max:test:" + object.id}, extra);
    if (member !== undefined) payload.member = member;
    return f.bridge.request(payload);
}

test("all 588 declared members and 1107 operations reach real adapter methods; previews do not write", () => {
    const f = fixture();
    let count = 0;
    let operations = 0;
    for (const [type, definition] of Object.entries(schema.objects)) {
        for (const [name, item] of Object.entries(definition.members)) {
            count++;
            f.advance(61000);
            for (const operation of item.access) {
                operations++;
                const extra = {};
                if (operation === "set") extra.value = sample(item, f.registry);
                if (operation === "call") extra.arguments = (item.argument_modes || [item.arguments])[0].filter(p => p.required !== false).map(p => sample(p, f.registry));
                const object = f.registry.byType[type];
                if (operation === "set") {
                    if (item.value_type === "bool") object.values[name] = !extra.value;
                    else if (item.value_type === "dictionary" && item.dictionary.native_schema_from_readback) {
                        object.values[name] = {probe: 0}; extra.value = {probe: 1};
                    } else if (typeof extra.value === "boolean") object.values[name] = !extra.value;
                    else if (typeof extra.value === "number") object.values[name] = extra.value + 1;
                    else if (typeof extra.value === "string") object.values[name] = "previous-value";
                    else if (Array.isArray(extra.value)) object.values[name] = [1];
                    else object.values[name] = 0;
                }
                const writeCount = f.registry.writes.length;
                const callCount = f.registry.calls.length;
                const readCount = f.registry.reads.length;
                const observationCount = f.registry.observed.length;
                const result = request(type, operation, name, extra, f);
                assert.ok(["ok", "preview", "no_change"].includes(result.status), type + "." + name + ":" + operation + " " + JSON.stringify(result));
                if (operation === "get") assert.equal(f.registry.reads.length, readCount + 1);
                if (operation === "set" || (operation === "call" && item.mutating)) {
                    assert.equal(f.registry.writes.length, writeCount, "preview must not write");
                    assert.equal(f.registry.calls.length, callCount, "preview must not call a mutating function");
                    const applied = request(type, operation, name, {...extra, dry_run: false}, f);
                    assert.equal(applied.status, "ok", type + "." + name + " apply " + JSON.stringify(applied));
                    if (operation === "set") assert.equal(f.registry.writes.length, writeCount + 1);
                    else assert.equal(f.registry.calls.length, callCount + 1);
                } else if (operation === "call") assert.equal(f.registry.calls.length, callCount + 1);
                if (operation === "observe") {
                    assert.equal(f.registry.observed.length, observationCount + 1);
                    assert.equal(f.bridge.request({op: "poll", subscription_id: result.subscription_id, owner: "test-owner"}).status, "ok");
                    assert.equal(f.bridge.request({op: "unobserve", subscription_id: result.subscription_id, owner: "test-owner"}).status, "ok");
                }
            }
        }
    }
    assert.equal(count, schema.member_count);
    assert.equal(count, 588);
    assert.equal(operations, 1107);
    assert.ok(f.registry.writes.length > 0, "every setter was actually dispatched after its preview");
    assert.ok(f.registry.calls.length > 0, "documented read functions reached the real adapter method");
});

test("native set is default-preview, identity/CAS guarded, boolean no-op and possible-write aware", () => {
    const f = fixture();
    const track = f.registry.byType.Track;
    track.values.mute = 0;
    assert.equal(request("Track", "set", "mute", {value: true}, f).status, "preview");
    assert.equal(f.registry.writes.length, 0);
    assert.equal(request("Track", "set", "mute", {value: true, expected_identity: "max:stale:1", dry_run: false}, f).code, "stale_identity");
    assert.equal(request("Track", "set", "mute", {value: true, expected_value: true, dry_run: false}, f).code, "stale_value");
    const result = request("Track", "set", "mute", {value: true, dry_run: false}, f);
    assert.equal(result.value, true);
    assert.equal(f.registry.writes.length, 1);
    assert.equal(request("Track", "set", "mute", {value: true, dry_run: false}, f).status, "no_change");
    assert.equal(f.registry.writes.length, 1);
    track.failSet = "mute";
    const failed = request("Track", "set", "mute", {value: false, dry_run: false}, f);
    assert.equal(failed.status, "partial");
    assert.equal(failed.write_may_have_occurred, true);
    assert.equal(failed.readback, false);
});

test("dictionary note calls preserve the JSON payload and do not replace IDs in the adapter", () => {
    const f = fixture();
    const notes = {notes: [{note_id: 17, start_time: 4.02}]};
    const preview = request("Clip", "call", "apply_note_modifications", {arguments: [notes]}, f);
    assert.equal(preview.status, "preview");
    assert.equal(f.registry.dictionaries.length, 0);
    const applied = request("Clip", "call", "apply_note_modifications", {arguments: [notes], dry_run: false}, f);
    assert.equal(applied.status, "ok");
    assert.deepEqual(f.registry.calls.at(-1).args[0].data, notes);
    const invalid = request("Clip", "call", "apply_note_modifications", {arguments: [{notes: [{note_id: 17, unexpected: 4}]}]}, f);
    assert.equal(invalid.status, "error");
    assert.equal(f.registry.calls.length, 1);
});

test("references resolve real objects and reject wrong class or foreign scope before any write", () => {
    const f = fixture();
    const view = f.registry.byType["Song.View"];
    const track = f.registry.byType.Track;
    view.values.selected_track = 0;
    const value = {$ref: "live_set tracks 0", object_type: "Track"};
    const result = request("Song.View", "set", "selected_track", {value, dry_run: false}, f);
    assert.equal(result.status, "ok");
    assert.deepEqual(f.registry.writes.at(-1).value, ["id", track.id]);
    const failed = request("Song.View", "set", "selected_track", {value: {$live_id: track.id, identity: "max:other:" + track.id}, dry_run: false}, f);
    assert.equal(failed.code, "stale_identity");
    assert.equal(f.registry.writes.length, 1);
    const wrong = f.bridge.request({op: "resolve", path: "id " + view.id, object_type: "Track"});
    assert.equal(wrong.code, "object_type_mismatch");
});

test("Live object arguments use id-tagged Max atoms; plain note IDs remain plain integers", () => {
    const f = fixture();
    const clip = f.registry.byType.Clip;
    const result = request("Track", "call", "duplicate_clip_to_arrangement", {
        arguments: [{$live_id: clip.id, identity: "max:test:" + clip.id, object_type: "Clip"}, 16],
        dry_run: false,
    }, f);
    assert.equal(result.status, "ok");
    assert.deepEqual(f.registry.calls.at(-1).args, ["id " + clip.id, 16]);
    assert.equal(request("Clip", "call", "remove_notes_by_id", {arguments: [[17, 18]], dry_run: false}, f).status, "ok");
    assert.deepEqual(f.registry.calls.at(-1).args, [[17, 18]]);
});

test("observations are actual callbacks, bounded, owned, TTL-expiring and explicitly cleaned", () => {
    const f = fixture();
    const watched = request("Track", "observe", "mute", {queue_limit: 2, ttl_seconds: 1}, f);
    assert.equal(watched.status, "ok");
    const api = f.registry.observers.at(-1);
    api.notify(1); api.notify(0); api.notify(1);
    const poll = f.bridge.request({op: "poll", owner: "test-owner", subscription_id: watched.subscription_id});
    assert.equal(poll.events.length, 2);
    assert.equal(poll.dropped, 2); // initial observation plus three notifications
    assert.equal(poll.events[1].value, true);
    assert.equal(f.bridge.request({op: "poll", owner: "other", subscription_id: watched.subscription_id}).code, "subscription_unavailable");
    f.advance(1001); f.bridge.sweep();
    assert.equal(api.property, "");
    assert.equal(api.id, 0);
    assert.equal(f.bridge.request({op: "poll", owner: "test-owner", subscription_id: watched.subscription_id}).close_reason, "ttl_expired");
    assert.equal(f.bridge.request({op: "cleanup", owner: "test-owner"}).removed, 1);
    assert.equal(f.bridge.activeCount(), 0);
});

test("capabilities enumerate exposed inherited members without claiming write verification", () => {
    const f = fixture();
    const result = request("PluginDevice", "capabilities", undefined, {limit: 1024}, f);
    assert.equal(result.status, "ok");
    assert.ok(result.members.some(m => m.name === "name" && m.declaring_type === "Device"));
    assert.ok(result.members.every(m => m.runtime_write_verified === false));
});

test("unsafe paths, nonfinite numbers, wrong access and unknown methods are rejected before native operations", () => {
    const f = fixture();
    assert.equal(f.bridge.request({op: "resolve", path: "live_set __class__", object_type: "Song"}).status, "error");
    assert.equal(request("Track", "call", "constructor", {}, f).status, "error");
    assert.equal(request("Track", "set", "devices", {value: []}, f).code, "operation_not_permitted");
    assert.equal(request("Song", "set", "tempo", {value: Infinity}, f).status, "error");
    assert.equal(f.registry.writes.length, 0);
    assert.equal(f.registry.calls.length, 0);
});

test("parameter setters enforce the actual native range before setting", () => {
    const f = fixture();
    f.registry.byType.DeviceParameter.values.min = 0;
    f.registry.byType.DeviceParameter.values.max = 1;
    assert.equal(request("DeviceParameter", "set", "value", {value: 2, dry_run: false}, f).status, "error");
    assert.equal(f.registry.writes.length, 0);
});

test("failed observer detachment retains its handle and cap occupancy until explicit cleanup succeeds", () => {
    const f = fixture();
    const watched = request("Track", "observe", "mute", {}, f);
    f.registry.byType.Track.failDetach = true;
    const stopped = f.bridge.request({op: "unobserve", subscription_id: watched.subscription_id, owner: "test-owner"});
    assert.equal(stopped.status, "partial");
    assert.equal(stopped.handle_retained, true);
    f.advance(40000); f.bridge.sweep();
    assert.equal(f.bridge.request({op: "poll", subscription_id: watched.subscription_id, owner: "test-owner"}).detach_failed, true);
    const cleanup = f.bridge.request({op: "cleanup", owner: "test-owner"});
    assert.deepEqual(cleanup.retained, [watched.subscription_id]);
    assert.equal(f.bridge.close().status, "partial");
    f.registry.byType.Track.failDetach = false;
    assert.equal(f.bridge.request({op: "cleanup", owner: "test-owner"}).removed, 1);
});

test("real loopback NDJSON keeps correlated framing and cleans disconnected owners", async () => {
    const forwarded = [];
    let server;
    server = createServer({sendToMax(id, raw) {
        const req = JSON.parse(raw); forwarded.push(req);
        setImmediate(() => server.response(id, JSON.stringify({status: "ok", op: req.op})));
    }});
    const address = await server.listen(0);
    assert.equal(address.address, "127.0.0.1");
    const socket = net.createConnection({host: "127.0.0.1", port: address.port});
    socket.setEncoding("utf8");
    await new Promise(resolve => socket.once("connect", resolve));
    const response = new Promise(resolve => socket.once("data", data => resolve(JSON.parse(data.trim()))));
    const frame = JSON.stringify({id: "same-id", request: {op: "resolve", owner: "test-owner"}}) + "\n";
    socket.write(frame.slice(0, 13)); socket.write(frame.slice(13));
    assert.deepEqual(await response, {id: "same-id", result: {status: "ok", op: "resolve"}});
    socket.destroy();
    await new Promise(resolve => setTimeout(resolve, 30));
    assert.ok(forwarded.some(req => req.op === "cleanup" && req.owner === "test-owner"));
    await server.close();
});

test("transport bounds oversized frames and timeout never retries a possible write", async () => {
    let calls = 0;
    const server = createServer({timeoutMs: 20, sendToMax() { calls++; }});
    const address = await server.listen(0);
    const socket = net.createConnection({host: "127.0.0.1", port: address.port});
    socket.setEncoding("utf8");
    await new Promise(resolve => socket.once("connect", resolve));
    const response = new Promise(resolve => socket.once("data", data => resolve(JSON.parse(data.trim()))));
    socket.write(JSON.stringify({id: "write", request: {op: "set", dry_run: false}}) + "\n");
    const failed = await response;
    assert.equal(failed.result.code, "max_request_timeout");
    assert.equal(failed.result.write_may_have_occurred, true);
    assert.equal(calls, 1);
    const closed = new Promise(resolve => socket.once("close", resolve));
    socket.write("x".repeat(MAX_BYTES + 2));
    await closed;
    assert.equal(calls, 1);
    await server.close();
});

test("transport timeout classifies readonly functions from the catalogue, not a caller flag", async () => {
    const server = createServer({schema, timeoutMs: 20, sendToMax() {}});
    const address = await server.listen(0);
    const socket = net.createConnection({host: "127.0.0.1", port: address.port});
    socket.setEncoding("utf8");
    await new Promise(resolve => socket.once("connect", resolve));
    const response = new Promise(resolve => socket.once("data", data => resolve(JSON.parse(data.trim()))));
    socket.write(JSON.stringify({id: "read", request: {op: "call", object_type: "MaxDevice",
        member: "get_bank_count", dry_run: false, mutating: true}}) + "\n");
    assert.equal((await response).result.write_may_have_occurred, false);
    socket.destroy();
    await server.close();
});

test("bootstrap diagnostics distinguish missing native readiness without creating LiveAPI", () => {
    const f = bootstrapFixture({"lom_schema.json": JSON.stringify(schema)});
    f.context.request("initial", JSON.stringify({op: "resolve", path: "live_set", object_type: "Song"}));
    assert.equal(f.reply().code, "max_device_not_ready");
    assert.equal(f.reply().bootstrap.ready_called, 0);
    assert.equal(f.reply().bootstrap.native_ready, false);
    assert.match(f.reply().detail, /live.thisdevice ready message/);
    f.context.request_readiness();
    assert.equal(f.reads.length, 0);
    assert.equal(f.nativeCreates(), 0);
});

test("bootstrap tries device folder then Max search path and keeps runtime identity on repeated ready", () => {
    const f = bootstrapFixture({"lom_schema.json": "\ufeff" + JSON.stringify(schema)});
    f.context.ready();
    assert.deepEqual(f.reads, ["/device-folder/lom_schema.json", "lom_schema.json"]);
    assert.equal(f.context.bootstrap_status().runtime_ready, true);
    assert.equal(f.nativeCreates(), 0, "factory initialization does not construct LiveAPI before an actual request");
    f.context.request("first", JSON.stringify({op: "resolve", path: "live_set", object_type: "Song"}));
    const identity = f.reply().target.identity;
    f.context.ready();
    f.context.request_readiness();
    f.context.request("again", JSON.stringify({op: "resolve", path: "live_set", object_type: "Song"}));
    assert.equal(f.reply().target.identity, identity);
    assert.equal(f.reads.length, 2, "duplicate readiness reannounces without reloading/resetting the runtime");
});

test("schema startup failure is retained in replies and a readiness request can retry read-only initialization", () => {
    const f = bootstrapFixture({});
    f.context.ready();
    f.context.request("failed", JSON.stringify({op: "resolve", path: "live_set", object_type: "Song"}));
    assert.equal(f.reply().bootstrap.ready_called, 1);
    assert.match(f.reply().detail, /Cannot load lom_schema.json/);
    assert.equal(f.reply().bootstrap.schema_attempts.length, 2);
    f.files["lom_schema.json"] = JSON.stringify(schema);
    f.context.request("ready", JSON.stringify({op: "readiness"}));
    assert.equal(f.reply().bootstrap.runtime_ready, true);
    assert.equal(f.reply().bootstrap.bootstrap_error, null);
    assert.equal(f.reply().bootstrap.ready_called, 1);
    assert.equal(f.nativeCreates(), 0);
});

test("unavailable real-object diagnostics retain checked/raw shapes while suppressing filesystem paths", () => {
    const bridge = createLomBridge(schema, {scope: "diagnostic-test", makeAPI() {
        return {id: "0", valid: 0, type: "Song", path: '"live_set"', unquotedpath: "C:/private/device.amxd",
            info: "id 0\ntype Song\nproperty tempo float\ndescription C:/private/file.als\nproperty name /private/name\n" + "x".repeat(5000)};
    }});
    const result = bridge.request({op: "resolve", path: "live_set", object_type: "Song"});
    assert.equal(result.code, "object_unavailable");
    assert.equal(result.write_may_have_occurred, false);
    assert.equal(result.diagnostics.checked_identity.id.js_type, "string");
    assert.equal(result.diagnostics.checked_identity.id.value, "0");
    assert.equal(result.diagnostics.valid.value, 0);
    assert.equal(result.diagnostics.path.value, "live_set");
    assert.deepEqual(result.diagnostics.info_fragments, ["id 0", "type Song", "property tempo float"]);
    assert.ok(!JSON.stringify(result).includes("C:/private"));
    assert.ok(!JSON.stringify(result).includes("/private/name"));
    assert.ok(JSON.stringify(result).length < 3000);
});

test("Live 12.4.1 valid=0 compatibility requires positive string ID and matching native info", () => {
    const f = fixture();
    f.registry.legacyValidZero = true;
    f.registry.byType.Song.values.tempo = 140;
    const resolved = f.bridge.request({op: "resolve", path: "live_set", object_type: "Song"});
    assert.equal(resolved.status, "ok");
    assert.equal(resolved.target.compatibility.native_valid.value, 0);
    assert.equal(resolved.target.compatibility.accepted_by, "nonzero_id_type_and_matching_info");
    const read = f.bridge.request({op: "get", path: "live_set", object_type: "Song", member: "tempo"});
    assert.equal(read.status, "ok");
    assert.equal(read.value, 140);
    assert.equal(f.registry.writes.length, 0);
});

test("ID zero, empty types and contradicting native info remain unavailable even with a positive valid flag", () => {
    for (const attributes of [
        {id: "0", valid: 0, type: "Song", info: "id 0\ntype Song"},
        {id: "1", valid: 0, type: "", info: "id 1\ntype Song"},
        {id: "1", valid: 0, type: "Song", info: "id 2\ntype Song"},
        {id: "1", valid: 1, type: "Song", info: "id 2\ntype Song"},
        {id: "1", valid: 0, type: "Song", info: "id 1\ntype Track"},
        {id: "1", valid: 0, type: "Song", info: ""},
    ]) {
        const bridge = createLomBridge(schema, {scope: "compat-test", makeAPI: () => attributes});
        const result = bridge.request({op: "resolve", path: "live_set", object_type: "Song"});
        assert.equal(result.code, "object_unavailable", JSON.stringify(attributes));
        assert.equal(result.write_may_have_occurred, false);
    }
});

test("scoped references share the same legacy-valid check and reject differing actual reference IDs before calls", () => {
    const f = fixture();
    f.registry.legacyValidZero = true;
    const clip = f.registry.byType.Clip;
    const args = [{$live_id: clip.id, identity: "max:test:" + clip.id, object_type: "Clip"}, 16];
    const accepted = request("Track", "call", "duplicate_clip_to_arrangement", {arguments: args, dry_run: false}, f);
    assert.equal(accepted.status, "ok");
    assert.deepEqual(f.registry.calls.at(-1).args, ["id " + clip.id, 16]);
    clip.id += 1; // The requested ID now resolves to a different native ID.
    const denied = request("Track", "call", "duplicate_clip_to_arrangement", {arguments: args, dry_run: false}, f);
    assert.equal(denied.code, "object_unavailable");
    assert.equal(f.registry.calls.length, 1);
});

test("native generic View is qualified from actual parent and documented child identity, never caller type", () => {
    const f = fixture(); f.registry.genericViews = true; f.registry.canonicalPaths = true; f.registry.legacyValidZero = true;
    for (const [canonical, expected] of [
        ["live_set view", "Song.View"], ["live_app view", "Application.View"],
        ["live_set tracks 0 view", "Track.View"], ["live_set tracks 0 devices 0 view", "Device.View"],
        ["live_set tracks 0 devices 1 view", "Eq8Device.View"],
    ]) {
        const result = f.bridge.request({op: "resolve", path: canonical, object_type: expected});
        assert.equal(result.status, "ok", JSON.stringify(result));
        assert.equal(result.target.object_type, expected);
        assert.equal(result.target.compatibility.view_qualification.child_id, f.registry.byPath[canonical].id);
    }
    assert.equal(f.bridge.request({op: "resolve", path: "live_set view", object_type: "Track.View"}).code, "object_type_mismatch");
    assert.equal(f.bridge.request({op: "resolve", path: "live_set tracks 0 devices 0 view", object_type: "Eq8Device.View"}).code, "object_type_mismatch");
    const inconsistent = fixture(); inconsistent.registry.genericViews = true; inconsistent.registry.canonicalPaths = true;
    inconsistent.registry.byType.Track.values.view = ["id", inconsistent.registry.byType["Song.View"].id];
    assert.equal(inconsistent.bridge.request({op: "resolve", path: "live_set tracks 0 view", object_type: "Track.View"}).code, "object_type_mismatch");
});

test("matching live paths tolerate cached info but stale metadata cannot authorize unknown mutating functions", () => {
    const f = fixture(); f.registry.genericViews = true; f.registry.canonicalPaths = true; f.registry.legacyValidZero = true;
    f.registry.byType["Song.View"].infoOverride = "id 1\ntype Song\nproperty tempo float";
    const resolved = f.bridge.request({op: "resolve", path: "live_set view", object_type: "Song.View"});
    assert.equal(resolved.status, "ok");
    assert.equal(resolved.target.compatibility.metadata_not_current, true);
    assert.equal(f.bridge.request({op: "get", path: "live_set view", object_type: "Song.View", member: "selected_track"}).status, "ok");
    const args = [{$ref: "live_set tracks 0 devices 0", object_type: "Device"}];
    assert.equal(f.bridge.request({op: "call", path: "live_set view", object_type: "Song.View", member: "select_device",
        arguments: args, expected_identity: resolved.target.identity}).status, "preview");
    const denied = f.bridge.request({op: "call", path: "live_set view", object_type: "Song.View", member: "select_device",
        arguments: args, expected_identity: resolved.target.identity, dry_run: false});
    assert.equal(denied.status, "unsupported");
    assert.equal(f.registry.calls.length, 0);
    const wrongPath = createLomBridge(schema, {scope: "wrong-path", makeAPI: () => ({id: "7", valid: 0, type: "View",
        path: "live_set tracks 0 view", unquotedpath: "live_set tracks 0 view", info: "id 7\ntype View"})});
    assert.equal(wrongPath.request({op: "resolve", path: "live_set view", object_type: "Song.View"}).code, "object_unavailable");
});

test("float32 post-write readback accepts only changed close representation, not no-ops or meaningful clamps", () => {
    const f = fixture(); const parameter = f.registry.byType.DeviceParameter;
    parameter.values.value = 0.5; parameter.float32Field = "value";
    const rounded = request("DeviceParameter", "set", "value", {value: 0.71, dry_run: false}, f);
    assert.equal(rounded.status, "ok");
    assert.equal(rounded.exact_match, false);
    assert.equal(rounded.verification_kind, "native_rounding");
    assert.equal(rounded.actual, Math.fround(0.71));
    parameter.values.value = 0.5;
    const tiny = request("DeviceParameter", "set", "value", {value: 0.5 + 1e-9, dry_run: false}, f);
    assert.equal(tiny.status, "partial");
    assert.equal(tiny.verification_kind, "no_observable_change");
    parameter.values.value = 0.5; parameter.float32Field = null; parameter.clampField = "value";
    const clamped = request("DeviceParameter", "set", "value", {value: 0.71, dry_run: false}, f);
    assert.equal(clamped.status, "partial");
    assert.equal(clamped.verification_kind, "mismatch");
});

test("nullable groove reference can be assigned, cleared and restored without resolving ID zero", () => {
    const f = fixture(); const clip = f.registry.byType.Clip; const groove = f.registry.byType.Groove;
    clip.values.groove = ["id", "0"];
    const reference = {$live_id: groove.id, identity: "max:test:" + groove.id, object_type: "Groove"};
    const assigned = request("Clip", "set", "groove", {value: reference, dry_run: false}, f);
    assert.equal(assigned.status, "ok");
    assert.equal(assigned.value.$live_id, groove.id);
    assert.deepEqual(f.registry.writes.at(-1).arguments, ["groove", "id", groove.id]);
    const preview = request("Clip", "set", "groove", {value: null}, f);
    assert.equal(preview.status, "preview");
    assert.equal(f.registry.writes.length, 1);
    const cleared = request("Clip", "set", "groove", {value: null, dry_run: false}, f);
    assert.equal(cleared.status, "ok");
    assert.equal(cleared.actual, null);
    assert.deepEqual(f.registry.writes.at(-1).value, ["id", 0]);
    assert.deepEqual(f.registry.writes.at(-1).arguments, ["groove", "id", 0]);
    assert.equal(request("Clip", "set", "groove", {value: assigned.value, dry_run: false}, f).status, "ok");
    assert.deepEqual(f.registry.writes.at(-1).arguments, ["groove", "id", groove.id]);
    assert.equal(f.registry.writes.length, 3);
    const badClass = {$live_id: f.registry.byType.Track.id, identity: "max:test:" + f.registry.byType.Track.id, object_type: "Track"};
    assert.equal(request("Clip", "set", "groove", {value: badClass, dry_run: false}, f).status, "error");
    assert.equal(f.registry.writes.length, 3);
    assert.equal(f.bridge.request({op: "resolve", path: "id 0", object_type: "Groove"}).status, "error");
    assert.ok(!f.registry.apiPaths.includes("id 0"));
});

test("null sentinels are accepted only by nullable references, never required function refs", () => {
    const f = fixture(); const clip = f.registry.byType.Clip;
    clip.values.groove = ["id", "0"];
    for (const value of [0, {$live_id: 0}, {$live_id: "0"}, {$ref: "id 0", object_type: "Groove"}]) {
        assert.equal(request("Clip", "set", "groove", {value, dry_run: false}, f).status, "no_change");
    }
    assert.equal(f.registry.writes.length, 0);
    for (const value of [null, 0, {$live_id: 0}]) {
        assert.equal(request("Track", "call", "duplicate_clip_to_arrangement", {arguments: [value, 16], dry_run: false}, f).status, "error");
    }
    assert.equal(f.registry.calls.length, 0);
    assert.ok(!f.registry.apiPaths.includes("id 0"));
    const codecSchema = JSON.parse(JSON.stringify(schema));
    const definition = codecSchema.objects.ClipSlot.members.duplicate_clip_to;
    definition.arguments[0].nullable = true;
    for (const mode of definition.argument_modes || []) mode[0].nullable = true;
    const calls = [];
    const bridge = createLomBridge(codecSchema, {scope: "nullable-codec", makeAPI: () => ({id: 11, valid: 1, type: "ClipSlot",
        info: "id 11\ntype ClipSlot\nfunction duplicate_clip_to", call: (...args) => { calls.push(args); return null; }})});
    assert.equal(bridge.request({op: "call", path: "id 11", object_type: "ClipSlot", member: "duplicate_clip_to",
        arguments: [null], expected_identity: "max:nullable-codec:11", dry_run: false}).status, "ok");
    assert.deepEqual(calls[0], ["duplicate_clip_to", "id 0"]);
});

test("signed nonzero native IDs resolve and round-trip references while zero, bounds, scope and class stay guarded", () => {
    const f = fixture(); f.registry.canonicalPaths = true; f.registry.legacyValidZero = true;
    const surface = f.registry.byType.ControlSurface; surface.id = -1; surface.canonicalPath = "control_surfaces 1";
    f.registry.byId[-1] = surface; f.registry.byPath["control_surfaces 1"] = surface;
    const resolved = f.bridge.request({op: "resolve", path: "id -1", object_type: "ControlSurface"});
    assert.equal(resolved.status, "ok");
    assert.equal(resolved.target.identity, "max:test:-1");
    assert.equal(f.bridge.request({op: "resolve", path: "id -1", object_type: "Track"}).code, "object_type_mismatch");
    const groove = f.registry.byType.Groove; groove.id = -2; f.registry.byId[-2] = groove;
    f.registry.byType.Clip.values.groove = ["id", 0];
    const reference = {$live_id: "-2", identity: "max:test:-2", object_type: "Groove"};
    const assigned = request("Clip", "set", "groove", {value: reference, dry_run: false}, f);
    assert.equal(assigned.status, "ok");
    assert.equal(assigned.value.$live_id, -2);
    assert.deepEqual(f.registry.writes.at(-1).value, ["id", -2]);
    assert.equal(request("Clip", "set", "groove", {value: {...reference, identity: "max:other:-2"}, dry_run: false}, f).code, "stale_identity");
    const called = request("ControlSurface", "call", "release_control", {
        arguments: [{$live_id: -1, identity: "max:test:-1", object_type: "ControlSurface"}], dry_run: false,
    }, f);
    assert.equal(called.status, "ok");
    assert.deepEqual(f.registry.calls.at(-1).args, ["id -1"]);
    assert.equal(f.bridge.request({op: "resolve", path: "id 0", object_type: "ControlSurface"}).status, "error");
    assert.equal(f.bridge.request({op: "resolve", path: "id -2147483649", object_type: "ControlSurface"}).status, "error");
    assert.equal(f.bridge.request({op: "resolve", path: "control_surfaces -1", object_type: "ControlSurface"}).status, "error");
    assert.equal(f.bridge.request({op: "resolve", path: "live_set tracks -1", object_type: "Track"}).status, "error");
});

test("this_device and parent aliases require a real canonical ID/type round trip", () => {
    const f = fixture(); f.registry.canonicalPaths = true; f.registry.legacyValidZero = true;
    const device = f.registry.byType.MaxDevice; const track = f.registry.byType.Track;
    device.canonicalPath = "live_set tracks 7 devices 3";
    track.canonicalPath = "live_set tracks 7";
    f.registry.byPath[device.canonicalPath] = device;
    f.registry.byPath[track.canonicalPath] = track;
    f.registry.byPath["this_device canonical_parent"] = track;
    f.registry.byPath["this_device canonical_parent canonical_parent"] = f.registry.byType.Song;
    const resolved = f.bridge.request({op: "resolve", path: "this_device", object_type: "Device"});
    assert.equal(resolved.status, "ok");
    assert.equal(resolved.target.object_type, "MaxDevice");
    assert.equal(resolved.target.compatibility.alias_resolution.canonical_lom_path, device.canonicalPath);
    const parent = f.bridge.request({op: "resolve", path: "this_device canonical_parent", object_type: "Track"});
    assert.equal(parent.status, "ok");
    assert.equal(parent.target.identity, "max:test:" + track.id);
    assert.equal(f.bridge.request({op: "resolve", path: "this_device canonical_parent canonical_parent", object_type: "Song"}).status, "ok");
    assert.equal(f.bridge.request({op: "resolve", path: "this_device", object_type: "Track"}).code, "object_type_mismatch");
    assert.equal(f.bridge.request({op: "resolve", path: "live_set tracks 0", object_type: "Track"}).code, "object_unavailable");
    f.registry.byPath[device.canonicalPath] = f.registry.byType.PluginDevice;
    assert.equal(f.bridge.request({op: "resolve", path: "this_device", object_type: "Device"}).code, "object_unavailable");
});

test("concrete registered controller roots qualify as ControlSurface without corrupting raw info checks", () => {
    const f = fixture(); f.registry.canonicalPaths = true; f.registry.legacyValidZero = true;
    const surface = f.registry.byType.ControlSurface;
    surface.id = -1; surface.rawType = "Komplete_Kontrol_S_Mk2"; surface.canonicalPath = "control_surfaces 1";
    f.registry.byId[-1] = surface; f.registry.byPath[surface.canonicalPath] = surface;
    for (const canonical of [surface.canonicalPath, "id -1"]) {
        const result = f.bridge.request({op: "resolve", path: canonical, object_type: "ControlSurface"});
        assert.equal(result.status, "ok");
        assert.equal(result.target.object_type, "ControlSurface");
        assert.equal(result.target.native_type, "Komplete_Kontrol_S_Mk2");
        assert.equal(result.target.compatibility.metadata_not_current, false);
        assert.equal(result.target.compatibility.registered_root_qualification.registered_root, surface.canonicalPath);
    }
    assert.equal(request("ControlSurface", "call", "get_control_names", {dry_run: false}, f).status, "ok");
    assert.equal(f.registry.calls.length, 1);
    assert.equal(request("ControlSurface", "call", "release_control", {
        arguments: [{$live_id: -1, identity: "max:test:-1", object_type: "ControlSurface"}], dry_run: false,
    }, f).status, "ok");
    assert.deepEqual(f.registry.calls.at(-1).args, ["id -1"]);
    assert.equal(f.bridge.request({op: "resolve", path: "id -1", object_type: "Device"}).code, "object_type_mismatch");
});

test("controller qualification rejects ordinary devices, children and inconsistent fresh registry roots", () => {
    const f = fixture(); f.registry.canonicalPaths = true;
    const surface = f.registry.byType.ControlSurface;
    surface.id = -1; surface.rawType = "Komplete_Kontrol_S_Mk2"; surface.canonicalPath = "control_surfaces 1";
    f.registry.byId[-1] = surface;
    const other = {...surface, id: -2};
    f.registry.byPath[surface.canonicalPath] = other;
    assert.equal(f.bridge.request({op: "resolve", path: "id -1", object_type: "ControlSurface"}).code, "object_unavailable");
    f.registry.byPath[surface.canonicalPath] = surface;
    surface.canonicalPath = "control_surfaces 1 view";
    assert.equal(f.bridge.request({op: "resolve", path: "id -1", object_type: "ControlSurface"}).code, "object_type_mismatch");
    const device = f.registry.byType.Device;
    device.canonicalPath = "control_surfaces 2"; f.registry.byPath[device.canonicalPath] = device;
    assert.equal(f.bridge.request({op: "resolve", path: device.canonicalPath, object_type: "ControlSurface"}).code, "object_type_mismatch");
    surface.canonicalPath = "live_set tracks 0 devices 0";
    assert.equal(f.bridge.request({op: "resolve", path: "id -1", object_type: "ControlSurface"}).code, "object_type_mismatch");
});

test("strict native binding rejects array, tagged-string and bare-ID reference setters", () => {
    const f = fixture(); const clip = f.registry.byType.Clip;
    const resolved = request("Clip", "resolve", undefined, {}, f);
    assert.equal(resolved.status, "ok");
    const api = f.registry.observers.at(-1);
    for (const value of [["id", 0], "id 0", 0]) {
        assert.throws(() => api.set("groove", value), /three arguments/);
    }
    assert.equal(f.registry.writes.length, 0);
    assert.deepEqual(clip.values.groove, sample(clip.members.groove, f.registry));
    api.set("groove", "id", 0);
    assert.deepEqual(f.registry.writes[0].arguments, ["groove", "id", 0]);
});

test("an ignored reference write reports no observable change for fresh dictionaries with the same native ID", () => {
    const f = fixture(); const clip = f.registry.byType.Clip; const groove = f.registry.byType.Groove;
    clip.values.groove = ["id", groove.id];
    clip.ignoreSet = "groove";
    const result = request("Clip", "set", "groove", {value: null, dry_run: false}, f);
    assert.equal(result.status, "partial");
    assert.equal(result.verified, false);
    assert.equal(result.write_may_have_occurred, true);
    assert.notStrictEqual(result.before, result.actual);
    assert.equal(result.before.$live_id, result.actual.$live_id);
    assert.equal(result.verification_kind, "no_observable_change");
    assert.equal(f.registry.writes.length, 1, "An unobserved reference write must not trigger a retry");
    assert.deepEqual(f.registry.writes[0].arguments, ["groove", "id", 0]);
});
