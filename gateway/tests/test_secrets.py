from pdl_gateway.secrets import hash_secret, new_secret, verify_secret


def test_hash_round_trip():
    encoded = hash_secret("hunter2")
    assert encoded.startswith("pbkdf2_sha256$200000$")
    assert verify_secret("hunter2", encoded)
    assert not verify_secret("hunter3", encoded)


def test_hash_is_salted():
    assert hash_secret("x") != hash_secret("x")


def test_malformed_encoding_is_false():
    assert not verify_secret("x", "not-a-hash")
    assert not verify_secret("x", "pbkdf2_sha256$abc$def")


def test_new_secret_is_long_and_unique():
    a, b = new_secret(), new_secret()
    assert a != b and len(a) >= 40


def test_wrong_algorithm_and_bad_iterations_are_false():
    encoded = hash_secret("x", iterations=1000)
    assert verify_secret("x", encoded)
    assert not verify_secret("x", encoded.replace("pbkdf2_sha256", "md5", 1))
    assert not verify_secret("x", encoded.replace("$1000$", "$0$", 1))
