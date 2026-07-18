"""Bounded decoders for demodulated Sub-GHz timing captures."""


def _median(values):
    values = sorted(values)
    if not values:
        return None
    return values[len(values) // 2]


def _classify_princeton_pair(mark, space, te):
    if mark <= 0 or space >= 0:
        return None
    mark = abs(mark)
    space = abs(space)
    short_min = te * 45 // 100
    short_max = te * 180 // 100
    long_min = te * 200 // 100
    long_max = te * 450 // 100
    if short_min <= mark <= short_max and long_min <= space <= long_max:
        return 0
    if long_min <= mark <= long_max and short_min <= space <= short_max:
        return 1
    return None


def decode_princeton_frames(timings, bit_count=24):
    """Return CRC-free Princeton key candidates ending at synchronization gaps."""
    if not 1 <= bit_count <= 64:
        raise ValueError("Princeton bit count must be 1..64")
    timings = tuple(int(value) for value in timings)
    required = bit_count * 2
    if len(timings) < required + 2:
        return ()

    short_candidates = [
        abs(value) for value in timings if 100 <= abs(value) <= 700
    ]
    te = _median(short_candidates)
    if te is None:
        return ()

    results = []
    for sync_index, value in enumerate(timings):
        if value >= 0 or abs(value) < te * 8:
            continue
        # A Princeton sync gap follows one short positive sync mark. The 24
        # data pairs immediately preceding that mark form the complete key.
        end = sync_index - 1
        start = end - required
        if start < 0 or timings[end] <= 0:
            continue
        frame_timings = timings[start:end]
        frame_short = [
            abs(item) for item in frame_timings if 100 <= abs(item) <= 700
        ]
        frame_te = _median(frame_short) or te
        key = 0
        valid = True
        for offset in range(start, end, 2):
            bit = _classify_princeton_pair(
                timings[offset], timings[offset + 1], frame_te
            )
            if bit is None:
                valid = False
                break
            key = (key << 1) | bit
        if valid:
            results.append(
                {
                    "protocol": "Princeton",
                    "key": key,
                    "bits": bit_count,
                    "te_us": frame_te,
                    "sync_us": abs(value),
                    "start": start,
                    "end": sync_index + 1,
                }
            )
    return tuple(results)
