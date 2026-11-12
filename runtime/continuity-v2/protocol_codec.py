"""Bounded native Codex request decoding; never changes decoded history bytes."""
import zstandard


class DecodeRejected(ValueError):
    pass


def decode_request(wire, encoding, *, max_wire=2*1024*1024, max_decoded=8*1024*1024):
    if not isinstance(wire, bytes) or len(wire) > max_wire:
        raise DecodeRejected('Encoded request exceeds the admitted size')
    if encoding is None or encoding == 'identity':
        decoded = wire
    elif encoding == 'zstd':
        try:
            declared = zstandard.frame_content_size(wire)
            if declared not in (zstandard.CONTENTSIZE_UNKNOWN, zstandard.CONTENTSIZE_ERROR) and declared > max_decoded:
                raise DecodeRejected('Decoded request exceeds the admitted size')
            # This installed binding enforces window size in bytes. Dividing by
            # 1024 rejected native streaming frames despite bounded output.
            decoded = zstandard.ZstdDecompressor(max_window_size=max(1024, max_decoded)).decompress(
                wire, max_output_size=max_decoded, allow_extra_data=False)
        except zstandard.ZstdError:
            raise DecodeRejected('Invalid, truncated or oversized zstd request') from None
    else:
        raise DecodeRejected('Unsupported Content-Encoding')
    if len(decoded) > max_decoded:
        raise DecodeRejected('Decoded request exceeds the admitted size')
    return decoded
