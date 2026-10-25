import struct

from nsa_convertor import extract_points_from_blob, extract_segments_from_blob, is_point_blob


def point_blob(points, flags=0):
    return b"".join(struct.pack("<2f2HI", x, y, round(size * 100), 100, flags) for x, y, size in points)


def legacy_blob(points, size=4.35):
    return b"".join(struct.pack("<7f", x1, y1, x2, y2, size, 1.0, 0)
                    for (x1, y1), (x2, y2) in zip(points, points[1:]))


def test_point_layout_is_decoded_as_points():
    blob = point_blob([(10, 20, 4.35), (12, 24, 4.2), (15, 30, 4.0)], flags=1)
    assert is_point_blob(blob)
    assert extract_points_from_blob(blob) == [(10, 20), (12, 24), (15, 30)]
    segments = extract_segments_from_blob(blob)
    assert [s[:4] for s in segments] == [(10, 20, 12, 24), (12, 24, 15, 30)]
    assert abs(segments[0][4] - 4.35) < 1e-6


def test_single_point_becomes_a_dot():
    blob = point_blob([(50, 60, 4.35)])
    assert is_point_blob(blob)
    (x1, y1, x2, y2, _size), = extract_segments_from_blob(blob)
    assert (x1, y1, y2) == (50, 60, 60) and abs(x2 - 50) < 0.1


def test_legacy_layout_is_unchanged():
    pts = [(1, 2), (3, 4), (5, 6), (7, 8)]
    blob = legacy_blob(pts)
    assert not is_point_blob(blob)
    assert extract_points_from_blob(blob) == pts


def test_ambiguous_length_resolved_by_chaining():
    # 112 bytes fits both 7 points (16 B) and 4 segments (28 B)
    legacy = legacy_blob([(0, 0), (1, 1), (2, 2), (3, 3), (4, 4)])
    points = point_blob([(k * 3, k * 5, 4.0) for k in range(7)])
    assert len(legacy) == len(points) == 112
    assert not is_point_blob(legacy)
    assert is_point_blob(points)
