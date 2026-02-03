default:
    @echo "Available recipes:"
    @echo "  build       - Build plugin with Decky CLI"
    @echo "  clean       - Remove build artifacts"
    @echo "  setup       - Install dependencies"

setup:
    .vscode/setup.sh

build:
    .vscode/build.sh

clean:
    rm -rf node_modules dist out
    sudo rm -rf /tmp/decky
