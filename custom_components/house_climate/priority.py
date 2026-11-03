"""Named room priorities, independent of the thermal model."""


def modes(settings):
    """Reconcile membership, preserving legacy weights only for migration."""
    configured = settings.get("priority_modes")
    if not configured:
        configured = {
            "balance": {m["entity_id"]: m.get("weight", 1) for m in settings["sensors"]}
        }
    return {
        name: {
            m["entity_id"]: weights.get(m["entity_id"], 1) for m in settings["sensors"]
        }
        for name, weights in configured.items()
    }


def members(settings, active):
    weights = modes(settings)[active]
    return [{**m, "weight": weights[m["entity_id"]]} for m in settings["sensors"]]
