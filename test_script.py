"""Simple script to test the MCP debugger."""

def greet(name):
    message = f"Hello, {name}!"
    print(message)
    return message


def main():
    names = ["Alice", "Bob", "Charlie"]
    results = []
    for name in names:
        result = greet(name)
        results.append(result)
    print(f"Done: {len(results)} greetings")


if __name__ == "__main__":
    main()
