/* Official LiveAPI adapter. LiveAPI objects are created only after ready(). */
/* ES5 syntax keeps the same source usable by Max's js object and Node tests. */
var autowatch = 0;
var inlets = 1;
var outlets = 1;

function createLomBridge(schema, dependencies) {
    "use strict";
    var deps = dependencies || {};
    var now = deps.now || function () { return Date.now(); };
    var scope = deps.scope || (Date.now().toString(16) + Math.random().toString(16).slice(2));
    var subscriptions = {};
    var temporaryDicts = [];
    var viewProofs = {};
    var viewProofOrder = [];
    var closed = false;
    var owns = function (obj, key) { return Object.prototype.hasOwnProperty.call(obj, key); };
    var objects = schema.objects;
    if (!objects || typeof objects !== "object") { throw new Error("Missing official object schema"); }

    function fault(message, code, diagnostics) {
        var error = new Error(message);
        error.code = code || "invalid_request";
        if (diagnostics) { error.diagnostics = diagnostics; }
        throw error;
    }
    function integer(value, label, low, high) {
        if (typeof value !== "number" || !isFinite(value) || Math.floor(value) !== value || value < low || value > high) {
            fault(label + " must be an integer in " + low + ".." + high);
        }
        return value;
    }
    function nativeObjectId(value, allowZero) {
        if (typeof value === "string" && /^-?\d+$/.test(value.trim())) { value = Number(value); }
        value = integer(value, "Live object ID", -2147483648, 2147483647);
        if (!allowZero && value === 0) { fault("Live object ID zero is absence, not a target"); }
        return value;
    }
    function jsonValue(value, depth) {
        var keys, index;
        depth = depth || 0;
        if (depth > 20) { fault("JSON nesting exceeds 20"); }
        if (value === null || typeof value === "boolean") { return; }
        if (typeof value === "number") { if (!isFinite(value)) { fault("Number must be finite"); } return; }
        if (typeof value === "string") { if (value.length > 65536) { fault("String exceeds limit"); } return; }
        if (Object.prototype.toString.call(value) === "[object Array]") {
            if (value.length > 4096) { fault("List exceeds 4096 items"); }
            for (index = 0; index < value.length; index++) { jsonValue(value[index], depth + 1); }
            return;
        }
        if (typeof value === "object") {
            keys = Object.keys(value);
            if (keys.length > 4096) { fault("Dictionary exceeds limit"); }
            for (index = 0; index < keys.length; index++) {
                if (keys[index] === "__proto__" || keys[index] === "prototype" || keys[index] === "constructor") { fault("Unsafe dictionary key"); }
                jsonValue(value[keys[index]], depth + 1);
            }
            return;
        }
        fault("Only finite JSON values are accepted");
    }
    function isA(actual, expected) {
        var seen = {};
        if (actual === expected) { return true; }
        while (actual && owns(objects, actual) && !seen[actual]) {
            seen[actual] = true;
            actual = objects[actual].parent;
            if (actual === expected) { return true; }
        }
        return false;
    }
    function member(owner, name, operation) {
        var seen = {}, definition, item;
        if (!owns(objects, owner)) { fault("Unknown official object type"); }
        if (typeof name !== "string") { fault("Member name must be a string"); }
        while (owner && owns(objects, owner) && !seen[owner]) {
            seen[owner] = true;
            definition = objects[owner];
            if (owns(definition.members || {}, name)) {
                item = definition.members[name];
                if ((item.access || []).indexOf(operation) < 0 && !(operation === "call" && item.kind === "functions")) {
                    fault("Official member does not permit " + operation, "operation_not_permitted");
                }
                return item;
            }
            owner = definition.parent;
        }
        fault("Unknown official member", "unknown_member");
    }
    function path(value) {
        var tokens, children = {}, owner, names, n, i, previousIndex = false;
        if (typeof value !== "string" || value.length > 4096) { fault("Invalid object path"); }
        tokens = value.trim().split(/\s+/);
        if (tokens.length === 2 && tokens[0] === "id" && /^-?\d+$/.test(tokens[1])) {
            return "id " + nativeObjectId(tokens[1], false);
        }
        if (tokens.length > 64 || ["live_set", "live_app", "control_surfaces", "this_device"].indexOf(tokens[0]) < 0) { fault("Unsupported path root"); }
        for (owner in objects) {
            if (!owns(objects, owner)) { continue; }
            names = Object.keys(objects[owner].members || {});
            for (n = 0; n < names.length; n++) {
                if (objects[owner].members[names[n]].kind === "children") { children[names[n]] = true; }
            }
        }
        children.canonical_parent = true;
        for (i = 1; i < tokens.length; i++) {
            if (/^\d+$/.test(tokens[i])) {
                if (previousIndex || (i === 1 && tokens[0] !== "control_surfaces")) { fault("Unexpected path index"); }
                integer(Number(tokens[i]), "Path index", 0, 2147483647);
                previousIndex = true;
            } else {
                if (!/^[A-Za-z][A-Za-z0-9_]*$/.test(tokens[i]) || !owns(children, tokens[i])) { fault("Path may traverse only official children"); }
                previousIndex = false;
            }
        }
        if (tokens[0] === "control_surfaces" && (tokens.length < 2 || !/^\d+$/.test(tokens[1]))) { fault("control_surfaces requires an index"); }
        return tokens.join(" ");
    }
    function diagnosticScalar(value, depth) {
        var index, result;
        depth = depth || 0;
        if (value === null || typeof value === "boolean") { return {js_type: typeof value, value: value}; }
        if (typeof value === "undefined") { return {js_type: "undefined", value: null}; }
        if (typeof value === "number") { return {js_type: "number", value: isFinite(value) ? value : "non_finite"}; }
        if (typeof value === "string") {
            return {js_type: "string", value: /^[A-Za-z0-9_. \-]{0,128}$/.test(value) ? value : "[non-identifier text suppressed]"};
        }
        if (Array.isArray(value)) {
            if (depth >= 2) { return {js_type: "array", value: "[nested array suppressed]", length: value.length}; }
            result = [];
            for (index = 0; index < value.length && index < 8; index++) {
                result.push(diagnosticScalar(value[index], depth + 1));
            }
            return {js_type: "array", value: result, length: value.length};
        }
        return {js_type: typeof value, value: "[non-scalar]"};
    }
    function diagnosticProperty(api, name) {
        try { return diagnosticScalar(api[name]); }
        catch (error) { return {js_type: "unavailable", error_type: /^[A-Za-z_]{1,64}$/.test(String(error.name)) ? String(error.name) : "Error"}; }
    }
    function diagnosticLomPath(api, name) {
        var value;
        try {
            value = api[name];
            if (Array.isArray(value)) { value = value.join(" "); }
            if (typeof value !== "string") { return diagnosticScalar(value); }
            value = value.replace(/["']/g, "").trim();
            return {js_type: "string", value: path(value)};
        } catch (error) { return {js_type: "unavailable", value: "[unavailable or non-LOM path suppressed]"}; }
    }
    function targetDiagnostics(api, canonical, checked) {
        var info = "", lines = [], fragments = [], index, text;
        try { info = String(api.info || ""); } catch (ignore) {}
        lines = info.split(/\r?\n/);
        for (index = 0; index < lines.length && fragments.length < 16; index++) {
            text = lines[index].trim();
            /* Only structural identifiers, never descriptions, values or filesystem paths. */
            if (/^(?:info\s+)?(?:id|type|property|function|child|children)\s+[A-Za-z0-9_. \-]+$/.test(text)) {
                fragments.push(text.slice(0, 160));
            }
        }
        return {backend: "max", requested_lom_path: canonical,
            checked_identity: checked || null,
            id: diagnosticProperty(api, "id"), valid: diagnosticProperty(api, "valid"),
            type: diagnosticProperty(api, "type"), path: diagnosticLomPath(api, "path"),
            unquotedpath: diagnosticLomPath(api, "unquotedpath"),
            info_characters: info.length, info_fragments: fragments};
    }
    function nativeCanonicalPath(api) {
        var candidates = ["unquotedpath", "path"], index, value;
        for (index = 0; index < candidates.length; index++) {
            try {
                value = api[candidates[index]];
                if (Array.isArray(value)) { value = value.join(" "); }
                if (typeof value !== "string" || !value.trim()) { continue; }
                return path(value.replace(/["']/g, "").trim());
            } catch (ignore) {}
        }
        return null;
    }
    function childReferenceId(value) {
        if (Array.isArray(value)) {
            if (value.length === 2 && value[0] === "id") { value = value[1]; }
            else if (value.length === 1) { value = value[0]; }
            else { return null; }
        }
        if (typeof value === "string" && /^id\s+-?\d+$/.test(value.trim())) { value = value.trim().slice(3).trim(); }
        try { return nativeObjectId(value, false); } catch (ignore) { return null; }
    }
    function qualifyNativeView(api, numericId, canonical) {
        var tokens, parentPath, parentApi, parentIdentity, viewMember, references, candidate, proof, key;
        if (!canonical || canonical.indexOf("id ") === 0) { fault("View namespace requires its actual canonical LOM path", "object_type_mismatch"); }
        tokens = canonical.split(" ");
        if (tokens[tokens.length - 1] !== "view") { fault("Native View does not have a confirmed view-child path", "object_type_mismatch"); }
        key = numericId + "|" + canonical;
        if (owns(viewProofs, key)) { return viewProofs[key]; }
        parentPath = tokens.slice(0, -1).join(" ");
        parentApi = deps.makeAPI(function () {}, parentPath);
        parentIdentity = liveIdentity(parentApi, parentPath);
        viewMember = member(parentIdentity.object_type, "view", "get");
        references = viewMember.ref_types || [];
        if (viewMember.kind !== "children" || !references.length || childReferenceId(parentApi.get("view")) !== numericId) {
            fault("Native parent does not identify this object as its documented view child", "object_type_mismatch");
        }
        candidate = parentIdentity.object_type + ".View";
        if (!owns(objects, candidate) || !references.some(function (type) { return isA(candidate, type); })) { candidate = references[0]; }
        if (!owns(objects, candidate) || candidate.slice(-5) !== ".View") { fault("No documented namespace for this native view", "object_type_mismatch"); }
        proof = {qualified_type: candidate, parent_type: parentIdentity.object_type, parent_lom_path: parentPath,
            parent_identity: "max:" + scope + ":" + parentIdentity.id, child_id: numericId,
            schema_view_reference: references.slice(0)};
        viewProofs[key] = proof; viewProofOrder.push(key);
        if (viewProofOrder.length > 128) { delete viewProofs[viewProofOrder.shift()]; }
        return proof;
    }
    function aliasPath(canonical) {
        return canonical.split(" ")[0] === "this_device" || canonical.split(" ").indexOf("canonical_parent") >= 0;
    }
    function proveAlias(canonical, actualPath, numericId, nativeType, depth) {
        var canonicalApi, identity;
        if (depth >= 3 || !actualPath || aliasPath(actualPath) || actualPath.indexOf("id ") === 0) {
            fault("Alias does not provide a stable canonical object path", "object_unavailable");
        }
        canonicalApi = deps.makeAPI(function () {}, actualPath);
        identity = liveIdentity(canonicalApi, actualPath, depth + 1);
        if (identity.id !== numericId || identity.native_type !== nativeType || nativeCanonicalPath(canonicalApi) !== actualPath) {
            fault("Alias canonical round trip changed the object identity or type", "object_unavailable");
        }
        return {requested_alias: canonical, canonical_lom_path: actualPath,
            canonical_identity: "max:" + scope + ":" + identity.id, native_type: identity.native_type};
    }
    function qualifyRegisteredSurface(numericId, nativeType, actualPath) {
        var rootApi, rootId, rootType;
        if (!actualPath || !/^control_surfaces \d+$/.test(actualPath) || owns(objects, nativeType) || nativeType === "View") { return null; }
        rootApi = deps.makeAPI(function () {}, actualPath);
        try { rootId = nativeObjectId(rootApi.id, false); rootType = String(rootApi.type || "").trim(); }
        catch (error) { fault("Registered ControlSurface root identity cannot be confirmed", "object_unavailable"); }
        if (rootId !== numericId || rootType !== nativeType || nativeCanonicalPath(rootApi) !== actualPath) {
            fault("Registered ControlSurface root round trip changed identity or native type", "object_unavailable");
        }
        return {public_type: "ControlSurface", native_type: nativeType, registered_root: actualPath,
            canonical_identity: "max:" + scope + ":" + rootId, accepted_by: "fresh_registered_root_identity"};
    }
    function liveIdentity(api, canonical, aliasDepth) {
        var nativeId, nativeValid, actual, checked, numericId, info = "", match, expression;
        var infoId = null, infoType = null, contradicts = false, compatibility = null;
        var actualPath, pathConfirmed, nativeType, viewProof = null, metadataNotCurrent, aliasProof = null, surfaceProof = null;
        aliasDepth = aliasDepth || 0;
        try {
            nativeId = api.id; nativeValid = api.valid; actual = String(api.type || "").trim();
            checked = {id: diagnosticScalar(nativeId), valid: diagnosticScalar(nativeValid), type: diagnosticScalar(actual)};
        } catch (identityError) {
            fault("Cannot read the requested live object's identity", "object_unavailable", targetDiagnostics(api, canonical, null));
        }
        try { numericId = nativeObjectId(nativeId, false); }
        catch (invalidId) { numericId = null; }
        if (numericId === null || !actual) {
            fault("No live object at the requested path", "object_unavailable", targetDiagnostics(api, canonical, checked));
        }
        try { info = String(api.info || ""); } catch (ignore) {}
        actualPath = nativeCanonicalPath(api);
        pathConfirmed = canonical.indexOf("id ") === 0 ? Number(canonical.slice(3)) === numericId : actualPath === canonical;
        if (canonical.indexOf("id ") === 0 && !pathConfirmed) {
            fault("Native object ID differs from the requested ID", "object_unavailable", targetDiagnostics(api, canonical, checked));
        }
        if (actualPath && canonical.indexOf("id ") !== 0 && actualPath !== canonical) {
            if (aliasPath(canonical)) {
                aliasProof = proveAlias(canonical, actualPath, numericId, actual, aliasDepth);
                pathConfirmed = true;
            } else {
                checked.actual_lom_path = actualPath;
                fault("Native object path differs from the requested LOM path", "object_unavailable", targetDiagnostics(api, canonical, checked));
            }
        }
        expression = /(?:^|\n)\s*(?:info\s+)?id\s+(-?\d+)\s*(?=\r?$|\n)/gm;
        while ((match = expression.exec(info)) !== null) {
            infoId = Number(match[1]);
            if (infoId !== numericId) { contradicts = true; }
        }
        expression = /(?:^|\n)\s*(?:info\s+)?type\s+([A-Za-z_][A-Za-z0-9_.]*)\s*(?=\r?$|\n)/gm;
        while ((match = expression.exec(info)) !== null) {
            infoType = match[1];
            if (infoType !== actual) { contradicts = true; }
        }
        checked.info_id = infoId;
        checked.info_type = infoType;
        if (contradicts && !pathConfirmed) {
            fault("Live object identity contradicts its native info", "object_unavailable", targetDiagnostics(api, canonical, checked));
        }
        metadataNotCurrent = contradicts || infoId === null || infoType === null;
        if (nativeValid === 0 || nativeValid === false || nativeValid === "0") {
            if (!pathConfirmed && (infoId !== numericId || infoType !== actual)) {
                fault("Native valid flag is false without matching ID/type info", "object_unavailable", targetDiagnostics(api, canonical, checked));
            }
            /* Live 12.4.1 Max returns valid=0 for otherwise consistent live objects. */
            compatibility = {native_valid: diagnosticScalar(nativeValid), accepted_by: pathConfirmed ? "nonzero_id_type_and_matching_path" : "nonzero_id_type_and_matching_info"};
        }
        nativeType = actual;
        if (actual === "View") {
            viewProof = qualifyNativeView(api, numericId, actualPath);
            actual = viewProof.qualified_type;
        } else {
            surfaceProof = qualifyRegisteredSurface(numericId, nativeType, actualPath);
            if (surfaceProof) { actual = "ControlSurface"; }
        }
        if (metadataNotCurrent || viewProof || aliasProof || surfaceProof) {
            compatibility = compatibility || {};
            compatibility.metadata_not_current = metadataNotCurrent;
            if (viewProof) { compatibility.view_qualification = viewProof; }
            if (aliasProof) { compatibility.alias_resolution = aliasProof; }
            if (surfaceProof) { compatibility.registered_root_qualification = surfaceProof; }
        }
        return {id: numericId, object_type: actual, native_type: nativeType, compatibility: compatibility,
            metadata_not_current: metadataNotCurrent, info_snapshot: info};
    }
    function target(request, callback) {
        var canonical = path(request.path), api = deps.makeAPI(callback || function () {}, canonical);
        var actual, expected = request.object_type, identity;
        if (!owns(objects, expected)) { fault("Unknown official object type"); }
        identity = liveIdentity(api, canonical);
        actual = identity.object_type;
        if (!isA(actual, expected) && !(expected === "this_device" && canonical === "this_device" && isA(actual, "Device"))) {
            fault("Expected " + expected + ", found " + actual, "object_type_mismatch");
        }
        api.mode = 0;
        return {api: api, path: canonical, object_type: actual, identity: "max:" + scope + ":" + identity.id,
            native_type: identity.native_type, metadata_not_current: identity.metadata_not_current,
            info_snapshot: identity.info_snapshot, compatibility: identity.compatibility, backend: "max"};
    }
    function publicTarget(resolved) {
        var result = {path: resolved.path, object_type: resolved.object_type, identity: resolved.identity, backend: "max"};
        if (resolved.native_type && resolved.native_type !== resolved.object_type) { result.native_type = resolved.native_type; }
        if (resolved.compatibility) { result.compatibility = resolved.compatibility; }
        return result;
    }
    function guard(request, resolved) {
        if (typeof request.expected_identity !== "string" || request.expected_identity !== resolved.identity) { fault("Target identity changed; resolve again", "stale_identity"); }
    }
    function owner(request) {
        if (typeof request.owner !== "string" || !/^[A-Za-z0-9:_-]{1,128}$/.test(request.owner)) { fault("A bounded owner token is required"); }
        return request.owner;
    }
    function exposed(resolved, item, preview) {
        var info = String(resolved.info_snapshot || ""), escaped, expression;
        if (resolved.metadata_not_current) {
            if (item.kind === "functions" && item.mutating !== false && !preview) {
                fault("Native info is not current; mutating function availability is unconfirmed", "unsupported");
            }
            return true; // Read/get/set preflight uses the actual API; stale info is not an availability oracle.
        }
        if (!info) { return true; } // Older builds do not expose complete info; native API remains authority.
        escaped = item.name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
        expression = new RegExp("(?:^|\\n)\\s*(?:info\\s+)?(?:property|function|child|children)\\s+" + escaped + "(?:\\s|$)");
        if (!expression.test(info)) { fault("Member is absent from this runtime's LiveAPI.info", "unsupported"); }
        return true;
    }
    function capabilityMembers(resolved, declared, requested, offset, limit) {
        var found = {}, types = {}, current = declared, name, list = [], item, supported;
        while (current && owns(objects, current) && !types[current]) {
            types[current] = true;
            for (name in objects[current].members) {
                if (!owns(objects[current].members, name) || owns(found, name)) { continue; }
                found[name] = true;
                item = objects[current].members[name];
                supported = null;
                if (resolved.info_snapshot && !resolved.metadata_not_current) {
                    try { exposed(resolved, item); supported = true; } catch (error) { supported = false; }
                }
                if (!requested || name === requested) {
                    list.push({name: name, declaring_type: current, kind: item.kind, access: item.access,
                        exposed: supported, runtime_write_verified: false});
                }
            }
            current = objects[current].parent;
        }
        list.sort(function (a, b) { return a.name < b.name ? -1 : a.name > b.name ? 1 : 0; });
        return {members: list.slice(offset, offset + limit), total: list.length,
            next_offset: offset + limit < list.length ? offset + limit : null};
    }
    function validateAgainst(spec, value, depth) {
        var type, i, keys, property, alternatives, last;
        depth = depth || 0;
        jsonValue(value, depth);
        if (!spec) { return value; }
        if (spec.oneOf) {
            alternatives = spec.oneOf;
            for (i = 0; i < alternatives.length; i++) {
                try { return validateAgainst(alternatives[i], value, depth + 1); } catch (error) { last = error; }
            }
            throw last || new Error("No argument alternative matched");
        }
        type = spec.value_type || spec.type || "native";
        if (type === "integer") { type = "int"; }
        if (type === "number") { type = "float"; }
        if (type === "boolean") { type = "bool"; }
        if (type === "string") { type = "symbol"; }
        if (type === "array") { type = "list"; }
        if (type === "object") { type = "dictionary"; }
        if (type === "reference" && (value === null || value === 0) && !spec.nullable) {
            fault("This documented object reference is not nullable");
        }
        if (value && typeof value === "object" && (owns(value, "$ref") || owns(value, "$live_id"))) {
            if (type !== "reference" && type !== "native" && !spec.$ref_types) { fault("A reference cannot replace this documented value type"); }
            return value;
        }
        if (spec.dictionary && !spec.dictionary.native_schema_from_readback) { validateAgainst(spec.dictionary, value, depth + 1); }
        if (type === "int" && (typeof value !== "number" || Math.floor(value) !== value)) { fault("Expected an integer"); }
        if (type === "float" && typeof value !== "number") { fault("Expected a finite number"); }
        if (type === "bool" && typeof value !== "boolean" && value !== 0 && value !== 1) { fault("Expected a boolean"); }
        if (type === "symbol" && typeof value !== "string") { fault("Expected a string"); }
        if (type === "list") {
            if (Object.prototype.toString.call(value) !== "[object Array]") { fault("Expected a list"); }
            for (i = 0; i < value.length; i++) { validateAgainst(spec.items, value[i], depth + 1); }
        }
        if (type === "reference" && value !== null && value !== 0) { fault("Expected a scoped object reference"); }
        if (type === "dictionary") {
            if (!value || typeof value !== "object" || Object.prototype.toString.call(value) === "[object Array]") { fault("Expected a dictionary"); }
            keys = Object.keys(value);
            for (i = 0; i < (spec.required || []).length; i++) { if (!owns(value, spec.required[i])) { fault("Required dictionary key is missing"); } }
            for (i = 0; i < keys.length; i++) {
                property = (spec.properties || {})[keys[i]];
                if (!property && spec.additionalProperties === false) { fault("Unknown dictionary key"); }
                if (property) { validateAgainst(property, value[keys[i]], depth + 1); }
            }
        }
        if (typeof value === "number") {
            if (typeof spec.minimum === "number" && value < spec.minimum) { fault("Value is below the documented minimum"); }
            if (typeof spec.maximum === "number" && value > spec.maximum) { fault("Value is above the documented maximum"); }
            if (typeof spec.exclusiveMinimum === "number" && value <= spec.exclusiveMinimum) { fault("Value must exceed the documented minimum"); }
        }
        if (spec.enum && spec.enum.indexOf(value) < 0) { fault("Value is not a documented enum"); }
        return value;
    }
    function objectReference(value, expectedTypes, nullable) {
        var resolved, nativeId, actual, api, identity;
        if (value === null || value === 0 || (value && typeof value === "object" &&
                ((owns(value, "$live_id") && (value.$live_id === 0 || value.$live_id === "0")) ||
                (owns(value, "$ref") && /^id\s+0$/.test(String(value.$ref).trim()))))) {
            if (!nullable) { fault("This documented object reference is not nullable"); }
            if (value && typeof value === "object" && value.object_type && expectedTypes && expectedTypes.length &&
                    !expectedTypes.some(function (type) { return isA(value.object_type, type); })) {
                fault("Null reference class is incompatible");
            }
            return 0; // Explicit absence; never construct/resolve a LiveAPI object at id 0.
        }
        if (value && typeof value === "object" && owns(value, "$ref")) {
            resolved = target({path: value.$ref, object_type: value.object_type});
            if (expectedTypes && expectedTypes.length && !expectedTypes.some(function (type) { return isA(resolved.object_type, type); })) { fault("Reference class is incompatible"); }
            return Number(resolved.api.id);
        }
        if (value && typeof value === "object" && owns(value, "$live_id")) {
            nativeId = nativeObjectId(value.$live_id, false);
            if (value.identity !== "max:" + scope + ":" + nativeId) { fault("Object reference belongs to another runtime", "stale_identity"); }
            api = deps.makeAPI(function () {}, "id " + nativeId);
            identity = liveIdentity(api, "id " + nativeId);
            if (identity.id !== nativeId) { fault("Referenced object ID changed", "object_unavailable"); }
            actual = identity.object_type;
            if (expectedTypes && expectedTypes.length && !expectedTypes.some(function (type) { return isA(actual, type); })) { fault("Reference class is incompatible"); }
            return nativeId;
        }
        fault("Expected an explicit scoped reference");
    }
    function fromNative(value, item) {
        var parsed, dictionary, i, refs, type, nativeId;
        if (typeof value === "undefined") { return null; }
        if (item && item.value_type === "bool" && (value === 0 || value === 1)) { return value === 1; }
        if (value && typeof value.stringify === "function") { return JSON.parse(value.stringify()); }
        if (typeof value === "string" && /^[\[{]/.test(value)) {
            try { parsed = JSON.parse(value); jsonValue(parsed); return parsed; } catch (ignore) {}
        }
        if (Object.prototype.toString.call(value) === "[object Array]") {
            if (value[0] === "dictionary" && value.length === 2 && deps.makeDict) {
                dictionary = deps.makeDict(value[1]);
                return JSON.parse(dictionary.stringify());
            }
            if (value.length >= 2 && value[0] === "id") {
                refs = [];
                type = item && item.ref_types && item.ref_types[0];
                if (!type && item && item.items && item.items.ref_types) { type = item.items.ref_types[0]; }
                for (i = 0; i < value.length; i += 2) {
                    if (value[i] !== "id") { return value; }
                    try { nativeId = nativeObjectId(value[i + 1], true); } catch (invalidId) { return value; }
                    refs.push(nativeId === 0 ? null : {$live_id: nativeId, object_type: type || null,
                        identity: "max:" + scope + ":" + nativeId});
                }
                return item && item.collection ? refs : (refs.length === 1 ? refs[0] : refs);
            }
            if (item && item.value_type !== "list" && value.length === 1) { return fromNative(value[0], item); }
        }
        jsonValue(value);
        return value;
    }
    function converted(value, spec, preview) {
        var dictionary;
        if ((spec && spec.value_type === "reference") || (value && typeof value === "object" && (owns(value, "$ref") || owns(value, "$live_id")))) {
            return objectReference(value, spec && (spec.ref_types || spec.$ref_types), !!(spec && spec.nullable));
        }
        if (value && typeof value === "object" && Object.prototype.toString.call(value) !== "[object Array]") {
            if (preview) { return value; }
            if (!deps.makeDict) { fault("Max dictionary support is unavailable", "unsupported"); }
            if (temporaryDicts.length >= 32) { fault("Temporary dictionary queue is full; wait before retrying", "resource_limit"); }
            dictionary = deps.makeDict();
            dictionary.parse(JSON.stringify(value));
            temporaryDicts.push({dict: dictionary, expires: now() + 60000});
            return dictionary;
        }
        if (Array.isArray(value)) {
            return value.map(function (entry) { return converted(entry, spec && spec.items, preview); });
        }
        if (typeof value === "boolean") { return value ? 1 : 0; }
        return value;
    }
    function argumentsFor(item, args, preview) {
        var specifications = item.arguments || [], modes = item.argument_modes || [specifications];
        var matched = null, error, index, mode, required, values = [];
        if (Object.prototype.toString.call(args) !== "[object Array]") { fault("arguments must be a list"); }
        jsonValue(args);
        function callValue(value, descriptor) {
            var nativeValue = converted(value, descriptor, preview);
            if ((descriptor && descriptor.value_type === "reference") ||
                    (value && typeof value === "object" && (owns(value, "$ref") || owns(value, "$live_id")))) {
                return "id " + nativeValue;
            }
            if (Array.isArray(value) && descriptor && descriptor.items && descriptor.items.value_type === "reference") {
                return nativeValue.map(function (identifier) { return "id " + identifier; });
            }
            return nativeValue;
        }
        if (item.native_signature_validation || item.signature_validation === "native_signature_validation") {
            return args.map(function (v) { return callValue(v, null); });
        }
        for (index = 0; index < modes.length; index++) {
            mode = modes[index];
            required = mode.filter(function (p) { return p.required !== false; }).length;
            if (args.length < required || args.length > mode.length) { continue; }
            try {
                args.forEach(function (v, i) { validateAgainst(mode[i], v); });
                matched = mode; break;
            } catch (problem) { error = problem; }
        }
        if (!matched) { throw error || new Error("Arguments do not match a documented signature"); }
        for (index = 0; index < args.length; index++) {
            if (item.dictionary && args[index] && typeof args[index] === "object" && !Array.isArray(args[index])) { validateAgainst(item.dictionary, args[index]); }
            values.push(callValue(args[index], matched[index]));
        }
        return values;
    }
    function read(resolved, item) {
        return fromNative(resolved.api.get(item.name), item);
    }
    function sameValue(actual, desired, item) {
        var actualId;
        if (item && item.value_type === "reference") {
            actualId = actual && typeof actual === "object" && owns(actual, "$live_id") ? actual.$live_id : 0;
            return actualId === objectReference(desired, item.ref_types, !!item.nullable);
        }
        if (item && item.value_type === "bool" && [true, false, 0, 1].indexOf(actual) >= 0 &&
                [true, false, 0, 1].indexOf(desired) >= 0) {
            return !!actual === !!desired;
        }
        return JSON.stringify(actual) === JSON.stringify(desired);
    }
    function stop(subscription, reason) {
        subscription.closed = true;
        subscription.close_reason = reason;
        subscription.closed_at = now();
        try {
            subscription.api.property = ""; subscription.api.id = 0;
            subscription.detach_failed = false; subscription.detach_error = null;
            return true;
        } catch (error) {
            subscription.detach_failed = true; subscription.detach_error = String(error);
            return false;
        }
    }
    function sweep() {
        var id, entry, retained = [];
        for (id in subscriptions) {
            if (!owns(subscriptions, id)) { continue; }
            entry = subscriptions[id];
            if (!entry.closed && entry.expires <= now()) { stop(entry, "ttl_expired"); }
            if (entry.closed && !entry.detach_failed && now() - entry.closed_at > 30000) { delete subscriptions[id]; }
        }
        temporaryDicts.forEach(function (entry) {
            if (entry.expires > now()) { retained.push(entry); }
            else { try { entry.dict.freepeer(); } catch (ignore) {} }
        });
        temporaryDicts = retained;
    }
    function cleanup(token) {
        var id, removed = 0, retained = [];
        for (id in subscriptions) {
            if (owns(subscriptions, id) && subscriptions[id].owner === token) {
                if (stop(subscriptions[id], "owner_cleanup")) { delete subscriptions[id]; removed++; }
                else { retained.push(id); }
            }
        }
        return {removed: removed, retained: retained};
    }
    function validateReadbackShape(before, proposed, depth) {
        var key;
        depth = depth || 0;
        if (depth > 20 || before === null) { return; }
        if (Array.isArray(before)) {
            if (!Array.isArray(proposed)) { fault("Value does not match the runtime dictionary list shape"); }
            if (before.length) { proposed.forEach(function (item) { validateReadbackShape(before[0], item, depth + 1); }); }
            return;
        }
        if (typeof before === "object") {
            if (!proposed || typeof proposed !== "object" || Array.isArray(proposed)) { fault("Value does not match the runtime dictionary shape"); }
            if (Object.keys(before).length) {
                for (key in proposed) {
                    if (!owns(proposed, key)) { continue; }
                    if (!owns(before, key)) { fault("Unknown runtime dictionary key"); }
                    validateReadbackShape(before[key], proposed[key], depth + 1);
                }
            }
            return;
        }
        if (typeof proposed !== typeof before) { fault("Value type does not match the runtime dictionary readback"); }
    }
    function handle(request) {
        var op, resolved, item, value, before, after, args, result, mutating, ownerToken;
        var id, entry, offset, limit, created, callback, writeAttempted = false;
        if (closed && ["cleanup", "poll", "unobserve"].indexOf(request.op) < 0) { fault("Bridge is closed", "bridge_closed"); }
        jsonValue(request);
        sweep();
        op = request.op;
        if (op === "cleanup") {
            result = cleanup(owner(request));
            result.status = result.retained.length ? "partial" : "ok";
            return result;
        }
        if (op === "poll" || op === "unobserve") {
            ownerToken = owner(request);
            id = request.subscription_id;
            if (typeof id !== "string" || !owns(subscriptions, id) || subscriptions[id].owner !== ownerToken) { fault("Unknown subscription for this owner", "subscription_unavailable"); }
            entry = subscriptions[id];
            if (op === "unobserve") {
                if (!stop(entry, "explicit_stop")) {
                    return {status: "partial", code: "observer_detach_failed", subscription_id: id,
                        closed: true, handle_retained: true, detail: entry.detach_error};
                }
                delete subscriptions[id]; return {status: "ok", subscription_id: id, closed: true};
            }
            limit = request.limit === undefined ? 100 : integer(request.limit, "limit", 1, 1024);
            return {status: "ok", subscription_id: id, events: entry.events.splice(0, limit),
                remaining: entry.events.length, dropped: entry.dropped, closed: entry.closed,
                close_reason: entry.close_reason || null, detach_failed: !!entry.detach_failed, target: entry.target};
        }
        if (["resolve", "get", "set", "call", "observe", "capabilities"].indexOf(op) < 0) { fault("Unsupported operation"); }
        resolved = target(request);
        if (op === "resolve") { return {status: "ok", target: publicTarget(resolved)}; }
        if (op === "capabilities") {
            offset = request.offset === undefined ? 0 : integer(request.offset, "offset", 0, 2147483647);
            limit = request.limit === undefined ? 100 : integer(request.limit, "limit", 1, 1024);
            result = capabilityMembers(resolved, request.object_type, request.member, offset, limit);
            result.status = "ok"; result.target = publicTarget(resolved);
            result.children = resolved.api.children || [];
            result.schema_member_count = schema.member_count || null;
            return result;
        }
        item = member(request.object_type, request.member, op === "observe" ? "observe" : op);
        exposed(resolved, item, op === "call" && request.dry_run !== false);
        if (op === "get") {
            value = read(resolved, item);
            offset = request.offset === undefined ? 0 : integer(request.offset, "offset", 0, 2147483647);
            limit = request.limit === undefined ? 100 : integer(request.limit, "limit", 1, 1024);
            if (Array.isArray(value)) {
                return {status: "ok", target: publicTarget(resolved), value: value.slice(offset, offset + limit),
                    total: value.length, next_offset: offset + limit < value.length ? offset + limit : null};
            }
            return {status: "ok", target: publicTarget(resolved), value: value};
        }
        if (op === "observe") {
            ownerToken = owner(request); guard(request, resolved);
            if (Object.keys(subscriptions).length >= 64) { fault("Subscription limit reached", "resource_limit"); }
            id = "max:" + scope + ":" + Math.random().toString(16).slice(2) + now().toString(16);
            entry = {owner: ownerToken, target: publicTarget(resolved), events: [], dropped: 0, closed: false,
                expires: now() + integer(request.ttl_seconds === undefined ? 60 : request.ttl_seconds, "ttl_seconds", 1, 300) * 1000,
                queue_limit: integer(request.queue_limit === undefined ? 128 : request.queue_limit, "queue_limit", 1, 256)};
            callback = function (notification) {
                if (entry.closed) { return; }
                if (!entry.api) { return; } // LiveAPI may notify while its constructor assigns a path.
                if (entry.expires <= now()) { stop(entry, "ttl_expired"); return; }
                if (!Number(entry.api.id)) { stop(entry, "object_unavailable"); return; }
                if (entry.events.length >= entry.queue_limit) { entry.events.shift(); entry.dropped++; }
                if (Array.isArray(notification) && notification[0] === item.name) { notification = notification.slice(1); }
                entry.events.push({timestamp_utc: new Date(now()).toISOString(), value: fromNative(notification, item)});
            };
            entry.api = deps.makeAPI(callback, "id " + Number(resolved.api.id));
            entry.api.mode = 0;
            subscriptions[id] = entry;
            try { entry.api.property = request.member; }
            catch (observationError) {
                if (stop(entry, "observer_registration_failed")) { delete subscriptions[id]; }
                return {status: entry.detach_failed ? "partial" : "unsupported", code: "observer_registration_failed", detail: String(observationError),
                    subscription_id: entry.detach_failed ? id : null, handle_retained: !!entry.detach_failed,
                    target: publicTarget(resolved), write_may_have_occurred: false};
            }
            return {status: "ok", subscription_id: id, target: entry.target, ttl_seconds: (entry.expires - now()) / 1000};
        }
        mutating = op === "set" || item.mutating !== false;
        if (mutating) { guard(request, resolved); }
        if (request.dry_run !== undefined && typeof request.dry_run !== "boolean") { fault("dry_run must be a boolean"); }
        if (op === "set") {
            if (item.dynamic_bounds) {
                item = JSON.parse(JSON.stringify(item));
                try {
                    item.minimum = Number(fromNative(resolved.api.get(item.dynamic_bounds.minimum), {value_type: "float"}));
                    item.maximum = Number(fromNative(resolved.api.get(item.dynamic_bounds.maximum), {value_type: "float"}));
                    if (!isFinite(item.minimum) || !isFinite(item.maximum) || item.minimum > item.maximum) { throw new Error("Invalid native range"); }
                } catch (rangeError) { fault("Cannot read finite native parameter range", "unsupported"); }
            }
            validateAgainst(item, request.value);
            before = read(resolved, item);
            if (item.dictionary && item.dictionary.native_schema_from_readback) { validateReadbackShape(before, request.value); }
            if (owns(request, "expected_value") && JSON.stringify(before) !== JSON.stringify(request.expected_value)) { fault("Value changed since snapshot", "stale_value"); }
            value = request.value;
            if (sameValue(before, value, item)) { return {status: "no_change", target: publicTarget(resolved), value: before, write_may_have_occurred: false}; }
            value = converted(value, item, request.dry_run !== false);
            if (request.dry_run !== false) { return {status: "preview", target: publicTarget(resolved), before: before, proposed: request.value,
                signature_validation: item.signature_validation || "catalogue_and_runtime", write_may_have_occurred: false}; }
            try {
                writeAttempted = true;
                /* LiveAPI.set uses separate id atoms; array or "id N" are not equivalent. */
                if (item.value_type === "reference") { resolved.api.set.apply(resolved.api, [item.name, "id", value]); }
                else { resolved.api.set(item.name, value); }
                after = read(resolved, item);
                var exactMatch = sameValue(after, request.value, item);
                var roundedMatch = !exactMatch && item.value_type === "float" && typeof after === "number" &&
                    typeof request.value === "number" && after !== before &&
                    Math.abs(after - request.value) <= Math.max(1e-9, 1e-7 * Math.max(Math.abs(after), Math.abs(request.value)));
                var unchangedAfterWrite = false;
                if (!exactMatch && !roundedMatch) {
                    try { unchangedAfterWrite = sameValue(after, before, item); } catch (ignoreComparison) {}
                }
                return {status: exactMatch || roundedMatch ? "ok" : "partial", target: publicTarget(resolved), before: before, value: after, actual: after,
                    verified: exactMatch || roundedMatch, exact_match: exactMatch,
                    verification_kind: exactMatch ? "exact" : roundedMatch ? "native_rounding" : unchangedAfterWrite ? "no_observable_change" : "mismatch",
                    write_may_have_occurred: true};
            } catch (error) {
                try { after = read(resolved, item); } catch (ignore) { after = null; }
                return {status: "partial", code: "set_failed_or_unverified", detail: String(error), target: publicTarget(resolved),
                    before: before, readback: after, write_may_have_occurred: writeAttempted};
            }
        }
        args = argumentsFor(item, request.arguments || [], mutating && request.dry_run !== false);
        if (mutating && request.dry_run !== false) {
            return {status: "preview", target: publicTarget(resolved), member: item.name,
                arguments: request.arguments || [], signature_validation: item.signature_validation || "catalogue", write_may_have_occurred: false};
        }
        try {
            writeAttempted = mutating;
            result = resolved.api.call.apply(resolved.api, [item.name].concat(args));
            return {status: "ok", target: publicTarget(resolved), result: fromNative(result, item),
                write_may_have_occurred: mutating, verification: mutating ? "native_return_only" : "read_only_call"};
        } catch (problem) {
            return {status: mutating ? "partial" : "error", code: "native_call_failed", detail: String(problem),
                target: publicTarget(resolved), write_may_have_occurred: writeAttempted};
        }
    }
    return {
        request: function (payload) {
            try { return handle(payload); }
            catch (error) {
                var response = {status: error.code === "unsupported" ? "unsupported" : "error",
                    code: error.code || "invalid_request", detail: String(error.message || error), write_may_have_occurred: false};
                if (error.diagnostics) { response.diagnostics = error.diagnostics; }
                return response;
            }
        },
        sweep: sweep,
        close: function () {
            var id, retained = []; closed = true;
            for (id in subscriptions) {
                if (!owns(subscriptions, id)) { continue; }
                if (stop(subscriptions[id], "bridge_closed")) { delete subscriptions[id]; }
                else { retained.push(id); }
            }
            temporaryDicts.forEach(function (entry) { try { entry.dict.freepeer(); } catch (ignore) {} });
            temporaryDicts = [];
            return {status: retained.length ? "partial" : "ok", retained: retained};
        },
        activeCount: function () { return Object.keys(subscriptions).filter(function (id) { return !subscriptions[id].closed; }).length; }
    };
}

/* Max entry points. The Node module export below does not initialize LiveAPI. */
var maxLomBridge = null;
var maxLomSweepTask = null;
var maxLomBootstrapError = null;
var maxLomBootstrapAttempts = [];
var maxLomNativeReady = false;
var readyCalled = 0;
function bootstrap_status() {
    return {native_ready: maxLomNativeReady, ready_called: readyCalled, runtime_ready: !!maxLomBridge,
        bootstrap_error: maxLomBootstrapError, schema_attempts: maxLomBootstrapAttempts.slice(0)};
}
function read_schema_file(filename) {
    var file = null, source = "", previousPosition, chunk;
    try {
        file = new File(filename, "read");
        if (!file.isopen) { throw new Error("File did not open"); }
        if (file.eof > 4 * 1024 * 1024) { throw new Error("Schema exceeds 4 MiB"); }
        while (file.position < file.eof) {
            previousPosition = file.position;
            chunk = file.readstring(Math.min(8192, file.eof - file.position));
            source += chunk;
            if (file.position <= previousPosition) { throw new Error("Schema read made no progress"); }
        }
        if (source.charAt(0) === "\ufeff") { source = source.slice(1); }
        return JSON.parse(source);
    } finally { if (file && file.isopen) { file.close(); } }
}
function initialize_ready_bridge(context) {
    var schema = null, filenames = [], devicePath, filename, i, errors = [];
    if (!maxLomNativeReady) { return false; }
    if (maxLomBridge) { return true; }
    maxLomBootstrapAttempts = [];
    try {
        if (context && context.patcher && context.patcher.filepath) {
            devicePath = String(context.patcher.filepath).replace(/\\/g, "/");
            filename = devicePath.slice(0, devicePath.lastIndexOf("/") + 1) + "lom_schema.json";
            if (filename !== "lom_schema.json") { filenames.push(filename); }
        }
        filenames.push("lom_schema.json");
        for (i = 0; i < filenames.length; i++) {
            try {
                schema = read_schema_file(filenames[i]);
                if (!schema || !schema.objects || typeof schema.objects !== "object") { throw new Error("Schema has no official objects map"); }
                maxLomBootstrapAttempts.push({filename: filenames[i], opened: true});
                break;
            } catch (fileError) {
                errors.push(filenames[i] + ": " + String(fileError));
                maxLomBootstrapAttempts.push({filename: filenames[i], opened: false, detail: String(fileError)});
                schema = null;
            }
        }
        if (!schema) { throw new Error("Cannot load lom_schema.json from device folder or Max search path. " + errors.join(" | ")); }
        maxLomBridge = createLomBridge(schema, {
            makeAPI: function (callback, target) { return new LiveAPI(callback, target); },
            makeDict: function (name) { return name ? new Dict(name) : new Dict(); }
        });
        maxLomBootstrapError = null;
        return true;
    } catch (error) {
        maxLomBridge = null;
        maxLomBootstrapError = String(error);
        return false;
    }
}
function ready() {
    readyCalled++;
    maxLomNativeReady = true;
    if (initialize_ready_bridge(this)) {
        outlet(0, "bridge_ready", "max");
    } else { outlet(0, "bridge_error", maxLomBootstrapError); }
}
function request_readiness() {
    /* Node may start after live.thisdevice's first bang. Reannounce, never reset scope. */
    if (maxLomNativeReady && initialize_ready_bridge(this)) { outlet(0, "bridge_ready", "max"); }
    outlet(0, "bridge_state", JSON.stringify(bootstrap_status()));
}
function request(id, json) {
    var result, payload;
    try {
        payload = JSON.parse(json);
        if (payload && payload.op === "readiness") {
            request_readiness.call(this);
            result = {status: "ok", bootstrap: bootstrap_status(), write_may_have_occurred: false};
        } else if (!maxLomBridge) {
            result = {status: "unsupported", code: "max_device_not_ready",
                detail: maxLomBootstrapError || (maxLomNativeReady ? "Native device is ready; runtime bootstrap has not completed" : "No live.thisdevice ready message has reached the deferred JS inlet"),
                bootstrap: bootstrap_status(), write_may_have_occurred: false};
        } else { result = maxLomBridge.request(payload); }
    } catch (error) {
        result = {status: "error", code: "invalid_json", detail: String(error), write_may_have_occurred: false};
    }
    outlet(0, "response", String(id), JSON.stringify(result));
    if (maxLomBridge && maxLomBridge.activeCount() && !maxLomSweepTask) {
        maxLomSweepTask = new Task(function () {
            maxLomBridge.sweep();
            if (!maxLomBridge.activeCount()) { maxLomSweepTask.cancel(); maxLomSweepTask = null; }
        }, this);
        maxLomSweepTask.interval = 1000;
        maxLomSweepTask.repeat();
    }
}
function freebang() {
    if (maxLomSweepTask) { maxLomSweepTask.cancel(); maxLomSweepTask = null; }
    if (maxLomBridge) { maxLomBridge.close(); maxLomBridge = null; }
    maxLomNativeReady = false;
}
if (typeof module !== "undefined" && module.exports) { module.exports = {createLomBridge: createLomBridge}; }
