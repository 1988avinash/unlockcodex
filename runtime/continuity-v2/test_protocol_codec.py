import unittest
import zstandard
from protocol_codec import decode_request, DecodeRejected


class RequestEncodingTests(unittest.TestCase):
    def test_plain_and_native_zstd_have_identical_decoded_bytes(self):
        raw = b'{"model":"gpt-test","input":"synthetic only"}'
        self.assertEqual(decode_request(raw, None), raw)
        self.assertEqual(decode_request(zstandard.ZstdCompressor().compress(raw), 'zstd'), raw)

    def test_native_sized_streaming_frame_is_accepted_with_bounded_window(self):
        raw=b'{"input":"'+b'x'*65536+b'"}'
        compressed=zstandard.ZstdCompressor(write_content_size=False).compress(raw)
        self.assertEqual(decode_request(compressed,'zstd'),raw)

    def test_large_declared_window_is_rejected_even_with_small_output(self):
        params=zstandard.ZstdCompressionParameters.from_level(3,window_log=20,write_content_size=0)
        compressor=zstandard.ZstdCompressor(compression_params=params).compressobj()
        wire=compressor.compress(b'x'*1024)+compressor.flush()
        with self.assertRaises(DecodeRejected):
            decode_request(wire,'zstd',max_decoded=65536)
        self.assertEqual(decode_request(wire,'zstd',max_decoded=1048576),b'x'*1024)

    def test_oversized_decompressed_requests_are_denied(self):
        for write_content_size in (True, False):
            compressed = zstandard.ZstdCompressor(write_content_size=write_content_size).compress(b'x'*4096)
            with self.subTest(write_content_size=write_content_size), self.assertRaises(DecodeRejected):
                decode_request(compressed, 'zstd', max_decoded=1024)

    def test_truncated_extra_and_unknown_encodings_are_denied(self):
        compressed = zstandard.ZstdCompressor().compress(b'synthetic')
        for data, encoding in [(compressed[:-1], 'zstd'), (compressed+b'extra','zstd'),
                (compressed+compressed,'zstd'), (b'plain', 'gzip'), (b'plain','zstd, gzip')]:
            with self.subTest(encoding=encoding, bytes=len(data)), self.assertRaises(DecodeRejected):
                decode_request(data, encoding)

    def test_limits_apply_to_plain_and_compressed_wire_bytes(self):
        with self.assertRaises(DecodeRejected):
            decode_request(b'x'*1025, None, max_wire=1024)
        with self.assertRaises(DecodeRejected):
            decode_request(b'x'*1025, None, max_decoded=1024)


if __name__ == '__main__':
    unittest.main()
