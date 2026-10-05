#!/usr/bin/env python3
"""Link budget between a ground station and a spacecraft (free space, clear sky).

The viewer works out the same numbers live (orbit.js LinkBudget); this module is the reference
the fleet job and the charts use, and the two are checked against each other.

    downlink  spacecraft -> station   EIRP = 10 log10(P_tx W) + G_tx            (spacecraft)
                                      G/T  = G_rx - 10 log10(T_sys)              (station)
    uplink    station -> spacecraft   EIRP = 10 log10(P_tx W) + G_station        (station)
                                      G/T  = the spacecraft's Rx G/T
    FSPL  = 20 log10(4 pi d f / c)                    free-space path loss (d m, f Hz)
    C/N0  = EIRP - FSPL - other losses + G/T + 228.6  (dB-Hz; -228.6 dBW/K/Hz is Boltzmann's k)
    Eb/N0 = C/N0 - 10 log10(data rate bps)
    margin = Eb/N0 - required Eb/N0

Other losses (the spacecraft's, from the Payloads sheet) stand for pointing, polarisation,
atmosphere and implementation losses, which this budget does not model separately.
"""
import math

C = 299792458.0                 # m/s
BOLTZMANN_DB = -228.6           # dBW/K/Hz


def fspl_db(range_km, freq_mhz):
    return 20 * math.log10(4 * math.pi * range_km * 1e3 * freq_mhz * 1e6 / C)


def budget(range_km, station, payload):
    """{"fspl_db", "down": {...} or None, "up": {...} or None} for one station / spacecraft
    pair at range_km. Each direction: eirp_dbw, cn0_dbhz, ebn0_db, margin_db; None when the
    inputs for it are missing (or the station is 1-way, for the uplink)."""
    f = station["freq_mhz"]
    out = {"fspl_db": fspl_db(range_km, f), "down": None, "up": None}
    loss = out["fspl_db"] + (payload.get("losses_db") or 0.0)
    have = lambda d, *keys: all(d.get(k) is not None for k in keys)
    if have(payload, "tx_w", "gain_dbi", "rate_bps", "ebn0_req_db") and have(station, "gain_dbi", "tsys_k"):
        eirp = 10 * math.log10(payload["tx_w"]) + payload["gain_dbi"]
        gt = station["gain_dbi"] - 10 * math.log10(station["tsys_k"])
        cn0 = eirp - loss + gt - BOLTZMANN_DB
        ebn0 = cn0 - 10 * math.log10(payload["rate_bps"])
        out["down"] = {"eirp_dbw": eirp, "gt_dbk": gt, "cn0_dbhz": cn0, "ebn0_db": ebn0,
                       "margin_db": ebn0 - payload["ebn0_req_db"]}
    if (station.get("link") == "2-way" and have(station, "tx_w", "gain_dbi")
            and have(payload, "gt_dbk", "up_rate_bps", "ebn0_req_db")):
        eirp = 10 * math.log10(station["tx_w"]) + station["gain_dbi"]
        cn0 = eirp - loss + payload["gt_dbk"] - BOLTZMANN_DB
        ebn0 = cn0 - 10 * math.log10(payload["up_rate_bps"])
        out["up"] = {"eirp_dbw": eirp, "gt_dbk": payload["gt_dbk"], "cn0_dbhz": cn0, "ebn0_db": ebn0,
                     "margin_db": ebn0 - payload["ebn0_req_db"]}
    return out


if __name__ == "__main__":
    # a worked example to check against by hand: S-band, 1000 km
    print(f"FSPL at 1000 km, 2250 MHz: {fspl_db(1000, 2250):.2f} dB (by hand 159.49)")
    st = {"freq_mhz": 2250, "gain_dbi": 35.0, "tsys_k": 150.0, "tx_w": 100.0, "link": "2-way"}
    pl = {"tx_w": 5.0, "gain_dbi": 6.0, "rate_bps": 1e6, "ebn0_req_db": 2.5, "gt_dbk": -25.0,
          "up_rate_bps": 2e3, "losses_db": 3.0}
    b = budget(1000, st, pl)
    for d in ("down", "up"):
        print(d, {k: round(v, 2) for k, v in b[d].items()})
