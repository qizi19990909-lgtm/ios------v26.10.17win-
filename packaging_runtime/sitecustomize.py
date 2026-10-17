"""Normalize IPv6 link-local Bonjour scopes for Windows socket resolution."""
import os
import socket


if os.name == "nt":
    try:
        from pymobiledevice3.bonjour import _Adapters
    except Exception:
        _Adapters = None

    if _Adapters is not None and not getattr(_Adapters, "_qizi_numeric_scope_patch", False):
        _original_pick_iface_for_ip = _Adapters.pick_iface_for_ip

        def _pick_iface_for_ip(self, ip_str, family, v6_scopeid):
            if family == socket.AF_INET6 and ip_str.lower().startswith("fe80:") and v6_scopeid:
                return str(int(v6_scopeid))
            return _original_pick_iface_for_ip(self, ip_str, family, v6_scopeid)

        _Adapters.pick_iface_for_ip = _pick_iface_for_ip
        _Adapters._qizi_numeric_scope_patch = True
