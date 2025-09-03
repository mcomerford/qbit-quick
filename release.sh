#!/bin/bash
set -euo pipefail

DOCKER_REPO="michaelcomerford1/qbit-quick"

# Colours for output
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[1;34m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

log_info "Building wheel with Poetry..."
poetry build

log_info "Extracting version from pyproject.toml using Poetry..."
VERSION=$(poetry version -s)
if [[ -z "$VERSION" ]]; then
    log_error "Failed to extract version from pyproject.toml"
    exit 1
fi
log_success "Version detected: $VERSION"

log_info "Checking wheel exists for version $VERSION..."
PKG_NAME=$(poetry version | awk '{print $1}')
WHEEL_FILE="dist/${PKG_NAME//-/_}-$VERSION-py3-none-any.whl"

if [[ ! -f "$WHEEL_FILE" ]]; then
    log_error "Expected wheel not found: $WHEEL_FILE"
    exit 1
fi
log_success "Wheel found: $WHEEL_FILE"

log_info "Checking if Docker is running..."
if ! docker info >/dev/null 2>&1; then
    log_error "Docker is not running or not accessible."
    log_info "Start Docker Desktop or your Docker daemon and try again."
    exit 1
fi
log_success "Docker is running."

log_info "Checking Docker login status..."
if ! grep -q "https://index.docker.io/v1/" ~/.docker/config.json 2>/dev/null; then
    log_error "You are not logged in to Docker Hub. Run 'docker login' and try again."
    exit 1
fi
log_success "Docker authentication confirmed."

log_info "Building Docker image: $DOCKER_REPO:$VERSION..."
docker build \
    --build-arg WHEEL_FILE="$(basename "$WHEEL_FILE")" \
    -t "$DOCKER_REPO:$VERSION" \
    -t "$DOCKER_REPO:latest" .

log_success "Docker image built and tagged."

log_info "Pushing Docker image: $DOCKER_REPO:$VERSION"
docker push "$DOCKER_REPO:$VERSION"

log_info "Pushing Docker image: $DOCKER_REPO:latest"
docker push "$DOCKER_REPO:latest"

log_success "Docker image pushed to Docker Hub successfully."