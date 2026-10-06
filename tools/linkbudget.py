#!/usr/bin/env python3
"""Link budgets between ground stations and spacecraft (free space, clear sky), radio by radio.

The viewer works out the same numbers live (orbit.js LinkBudget); this module is the reference
the fleet job and the charts use, and the two are checked against each other.

Radios. A spacecraft can carry several (spacecraft.xlsx, sheet Radios: S-band TT&C, X-band
science, Ka-band ...) and a ground station can have several (groundstations, sheet Station
radios). A station radio and a spacecraft radio work together when their downlink frequencies
are in the same band (band_of: the IEEE letter bands, below); each such pair is a link with its
own budget. The downlink budget uses the spacecraft radio's downlink frequency, the uplink
budget its uplink frequency (when a radio leaves a frequency blank, the other end's is used).

    downlink  spacecraft -> station   EIRP = 10 log10(P_tx W) + G_tx            (spacecraft)
                                      G/T  = G_rx - 10 log10(T_sys)              (station)
    uplink    station -> spacecraft   EIRP = 10 log10(P_tx W) + G_station        (station)
                                      G/T  = the spacecraft's Rx G/T
    FSPL  = 20 log10(4 pi d f / c)                    free-space path loss (d m, f Hz)
    C/N0  = EIRP - FSPL - other losses + G/T + 228.6  (dB-Hz; -228.6 dBW/K/Hz is Boltzmann's k)
    Eb/N0 = C/N0 - 10 log10(data rate bps)
    margin = Eb/N0 - required Eb/N0

Other losses (the spacecraft radio's) stand for pointing, polarisation, atmosphere and
implementation losses, which this budget does not model separately.
"""
import math

C = 299792458.0                 # m/s
BOLTZMANN_DB = -228.6           # dBW/K/Hz
# IEEE Std 521 letter bands (MHz, lower edge inclusive); below 300 MHz VHF
BANDS = [(300, "UHF"), (1000, "L"), (2000, "S"), (4000, "C"), (8000, "X"), (12000, "Ku"), (18000, "K"),
         (27000, "Ka"), (40000, "V"), (75000, "W"), (110000, "mm")]


def band_of(freq_mhz):
    """The letter band of a frequency (MHz), or None without one."""
    if freq_mhz is None:
        return None
    name = "VHF"
    for lower, band in BANDS:
        if freq_mhz >= lower:
            name = band
    return name


def fspl_db(range_km, freq_mhz):
    return 20 * math.log10(4 * math.pi * range_km * 1e3 * freq_mhz * 1e6 / C)


def pairs(station_radios, craft_radios):
    """[(station radio, spacecraft radio, band)] for every pair in the same downlink band. A
    spacecraft radio without a downlink frequency (an old Payloads row) pairs with every
    station radio, at the station's frequency."""
    out = []
    for sr in station_radios:
        for cr in craft_radios:
            band = band_of(cr.get("down_mhz")) if cr.get("down_mhz") is not None else band_of(sr.get("down_mhz"))
            if band is not None and band == band_of(sr.get("down_mhz")):
                out.append((sr, cr, band))
    return out


def budget(range_km, station_radio, craft_radio, link="2-way"):
    """{"down": {...} or None, "up": {...} or None} for one station radio / spacecraft radio
    pair at range_km. Each direction: freq_mhz, fspl_db, eirp_dbw, gt_dbk, cn0_dbhz, ebn0_db,
    margin_db; None when an input for it is missing (or the station is 1-way, for the uplink)."""
    st, sc = station_radio, craft_radio
    have = lambda d, *keys: all(d.get(k) is not None for k in keys)
    losses = sc.get("losses_db") or 0.0
    out = {"down": None, "up": None}
    f_down = sc.get("down_mhz") if sc.get("down_mhz") is not None else st.get("down_mhz")
    if f_down is not None and have(sc, "tx_w", "gain_dbi", "rate_bps", "ebn0_req_db") and have(st, "gain_dbi", "tsys_k"):
        fspl = fspl_db(range_km, f_down)
        eirp = 10 * math.log10(sc["tx_w"]) + sc["gain_dbi"]
        gt = st["gain_dbi"] - 10 * math.log10(st["tsys_k"])
        cn0 = eirp - fspl - losses + gt - BOLTZMANN_DB
        ebn0 = cn0 - 10 * math.log10(sc["rate_bps"])
        out["down"] = {"freq_mhz": f_down, "fspl_db": fspl, "eirp_dbw": eirp, "gt_dbk": gt, "cn0_dbhz": cn0,
                       "ebn0_db": ebn0, "margin_db": ebn0 - sc["ebn0_req_db"]}
    f_up = next((f for f in (sc.get("up_mhz"), st.get("up_mhz"), f_down) if f is not None), None)
    if (link == "2-way" and f_up is not None and have(st, "tx_w", "gain_dbi")
            and have(sc, "gt_dbk", "up_rate_bps", "ebn0_req_db")):
        fspl = fspl_db(range_km, f_up)
        eirp = 10 * math.log10(st["tx_w"]) + st["gain_dbi"]
        cn0 = eirp - fspl - losses + sc["gt_dbk"] - BOLTZMANN_DB
        ebn0 = cn0 - 10 * math.log10(sc["up_rate_bps"])
        out["up"] = {"freq_mhz": f_up, "fspl_db": fspl, "eirp_dbw": eirp, "gt_dbk": sc["gt_dbk"], "cn0_dbhz": cn0,
                     "ebn0_db": ebn0, "margin_db": ebn0 - sc["ebn0_req_db"]}
    return out


if __name__ == "__main__":
    # worked examples to check against by hand
    print(f"FSPL at 1000 km, 2250 MHz: {fspl_db(1000, 2250):.2f} dB (by hand 159.49)")
    print("bands:", {f: band_of(f) for f in (400, 1575, 2250, 5000, 8450, 14000, 22000, 32000)})
    st = {"down_mhz": 2250, "up_mhz": 2050, "gain_dbi": 35.0, "tsys_k": 150.0, "tx_w": 100.0}
    sc = {"down_mhz": 2250, "up_mhz": 2050, "tx_w": 5.0, "gain_dbi": 6.0, "rate_bps": 1e6, "ebn0_req_db": 2.5,
          "gt_dbk": -25.0, "up_rate_bps": 2e3, "losses_db": 3.0}
    b = budget(1000, st, sc)
    for d in ("down", "up"):
        print(d, {k: round(v, 2) for k, v in b[d].items()})
