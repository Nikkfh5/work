from hello import greet


def test_greet_world():
    assert greet("World") == "Hello, World!"


def test_greet_empty():
    assert greet("") == "Hello, !"


def test_greet_alice():
    assert greet("Alice") == "Hello, Alice!"
