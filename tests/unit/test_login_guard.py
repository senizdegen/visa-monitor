from alay.adapters.vfs_global import login_allowed


def test_vfs_host_never_allowed():
    url = "https://visa.vfsglobal.com/tur/en/fra/login"
    assert login_allowed(url, ["mock-vfs", "localhost"]) is False


def test_mock_host_allowed():
    assert login_allowed("http://mock-vfs:8001/available/login", ["mock-vfs"]) is True
    assert login_allowed("http://localhost/x", ["mock-vfs", "localhost"]) is True
    assert login_allowed("http://mock-vfs.evil.com/x", ["mock-vfs"]) is False
