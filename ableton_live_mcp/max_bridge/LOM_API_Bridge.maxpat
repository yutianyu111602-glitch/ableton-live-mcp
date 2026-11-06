{
  "patcher": {
    "fileversion": 1,
    "appversion": {
      "major": 8,
      "minor": 6,
      "revision": 5,
      "architecture": "x64",
      "modernui": 1
    },
    "classnamespace": "box",
    "rect": [
      80,
      80,
      700,
      390
    ],
    "openinpresentation": 0,
    "default_fontsize": 12.0,
    "default_fontface": 0,
    "default_fontname": "Arial",
    "gridonopen": 1,
    "gridsize": [
      15.0,
      15.0
    ],
    "boxes": [
      {
        "box": {
          "id": "midi-in",
          "maxclass": "newobj",
          "text": "midiin",
          "patching_rect": [
            30,
            270,
            60,
            22
          ]
        }
      },
      {
        "box": {
          "id": "midi-out",
          "maxclass": "newobj",
          "text": "midiout",
          "patching_rect": [
            30,
            320,
            60,
            22
          ]
        }
      },
      {
        "box": {
          "id": "device-ready",
          "maxclass": "newobj",
          "text": "live.thisdevice",
          "patching_rect": [
            30,
            80,
            100,
            22
          ]
        }
      },
      {
        "box": {
          "id": "ready-defer",
          "maxclass": "newobj",
          "text": "deferlow",
          "patching_rect": [
            30,
            120,
            80,
            22
          ]
        }
      },
      {
        "box": {
          "id": "ready-message",
          "maxclass": "message",
          "text": "ready",
          "patching_rect": [
            30,
            160,
            60,
            22
          ]
        }
      },
      {
        "box": {
          "id": "node",
          "maxclass": "newobj",
          "text": "node.script node_bridge.js @autostart 1 @defer 1 @restart 0",
          "patching_rect": [
            180,
            80,
            430,
            22
          ]
        }
      },
      {
        "box": {
          "id": "request-defer",
          "maxclass": "newobj",
          "text": "deferlow",
          "patching_rect": [
            180,
            125,
            80,
            22
          ]
        }
      },
      {
        "box": {
          "id": "runtime",
          "maxclass": "newobj",
          "text": "js max_lom_runtime.js",
          "patching_rect": [
            180,
            205,
            180,
            22
          ]
        }
      },
      {
        "box": {
          "id": "label",
          "maxclass": "comment",
          "text": "LOM API Bridge \u2014 local loopback only",
          "patching_rect": [
            30,
            20,
            500,
            22
          ]
        }
      },
      {
        "box": {
          "id": "note",
          "maxclass": "comment",
          "text": "Load one instance in a dedicated test Set. Default writes are previews.",
          "patching_rect": [
            30,
            45,
            560,
            22
          ]
        }
      },
      {
        "box": {
          "id": "close",
          "maxclass": "newobj",
          "text": "closebang",
          "patching_rect": [
            420,
            270,
            80,
            22
          ]
        }
      },
      {
        "box": {
          "id": "shutdown",
          "maxclass": "message",
          "text": "shutdown",
          "patching_rect": [
            420,
            310,
            80,
            22
          ]
        }
      }
    ],
    "lines": [
      {
        "patchline": {
          "source": [
            "midi-in",
            0
          ],
          "destination": [
            "midi-out",
            0
          ]
        }
      },
      {
        "patchline": {
          "source": [
            "device-ready",
            0
          ],
          "destination": [
            "ready-defer",
            0
          ]
        }
      },
      {
        "patchline": {
          "source": [
            "ready-defer",
            0
          ],
          "destination": [
            "ready-message",
            0
          ]
        }
      },
      {
        "patchline": {
          "source": [
            "ready-message",
            0
          ],
          "destination": [
            "runtime",
            0
          ]
        }
      },
      {
        "patchline": {
          "source": [
            "node",
            0
          ],
          "destination": [
            "request-defer",
            0
          ]
        }
      },
      {
        "patchline": {
          "source": [
            "request-defer",
            0
          ],
          "destination": [
            "runtime",
            0
          ]
        }
      },
      {
        "patchline": {
          "source": [
            "runtime",
            0
          ],
          "destination": [
            "node",
            0
          ]
        }
      },
      {
        "patchline": {
          "source": [
            "close",
            0
          ],
          "destination": [
            "shutdown",
            0
          ]
        }
      },
      {
        "patchline": {
          "source": [
            "shutdown",
            0
          ],
          "destination": [
            "node",
            0
          ]
        }
      }
    ],
    "dependency_cache": [
      {
        "name": "max_lom_runtime.js",
        "type": "TEXT",
        "implicit": 1
      },
      {
        "name": "node_bridge.js",
        "type": "TEXT",
        "implicit": 1
      },
      {
        "name": "lom_schema.json",
        "type": "JSON",
        "implicit": 1
      }
    ],
    "autosave": 0
  }
}
