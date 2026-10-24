"""The supported tool parameter contract (a small JSON Schema subset)."""


def object_schema(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


TEXT = {"type": "string"}
NONEMPTY = {"type": "string", "minLength": 1}
TOOL_PARAMETERS = {
    "list_files": object_schema({"path": NONEMPTY}),
    "read_file": object_schema({"path": NONEMPTY, "start": {"type": "integer", "minimum": 1}, "end": {"type": "integer", "minimum": 1}}, ("path",)),
    "search": object_schema({"pattern": NONEMPTY, "path": NONEMPTY}, ("pattern",)),
    "run_shell": object_schema({"command": NONEMPTY, "timeout": {"type": "integer", "minimum": 1, "maximum": 120}}, ("command",)),
    "write_file": object_schema({"path": NONEMPTY, "content": TEXT}, ("path", "content")),
    "patch_file": object_schema({"path": NONEMPTY, "old_text": NONEMPTY, "new_text": TEXT}, ("path", "old_text", "new_text")),
    "delegate": object_schema({"task": NONEMPTY, "max_steps": {"type": "integer", "minimum": 1}}, ("task",)),
}
for _tool in ("write_file", "patch_file"):
    TOOL_PARAMETERS[_tool]["properties"]["expected_hash"] = NONEMPTY


def validate_parameters(schema, args):
    """Validate types, required fields, enums and bounds without coercing JSON."""
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    properties = schema["properties"]
    for key in schema.get("required", ()):
        if key not in args:
            raise ValueError(f"missing {key}")
    if schema.get("additionalProperties") is False:
        unknown = set(args) - set(properties)
        if unknown:
            raise ValueError(f"unknown argument: {sorted(unknown, key=str)[0]}")
    for key, value in args.items():
        rule = properties.get(key, {})
        kind = rule.get("type")
        if kind == "integer" and type(value) is not int:
            raise ValueError(f"{key} must be an integer")
        if kind == "string" and not isinstance(value, str):
            raise ValueError(f"{key} must be a string")
        if "enum" in rule and value not in rule["enum"]:
            raise ValueError(f"{key} must be one of {rule['enum']}")
        if isinstance(value, str) and len(value) < rule.get("minLength", 0):
            raise ValueError(f"{key} must not be empty")
        if type(value) is int:
            if "minimum" in rule and value < rule["minimum"]:
                raise ValueError(f"{key} must be >= {rule['minimum']}")
            if "maximum" in rule and value > rule["maximum"]:
                raise ValueError(f"{key} must be <= {rule['maximum']}")
