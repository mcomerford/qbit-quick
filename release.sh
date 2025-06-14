#!/bin/bash
set -euo pipefail

DOCKER_USERNAME="michaelcomerford1"
IMAGE_NAME="qbit-quick"

# Colours for output
GREEN='\033[0;32m'
BLUE='\033[1;34m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_info "Extracting version from pyproject.toml using Poetry..."
VERSION=$(poetry version -s)
log_success "Version detected: ${VERSION}"

log_info "Building Docker image: ${IMAGE_NAME}:${VERSION}"
docker build -t "${IMAGE_NAME}:${VERSION}" .

log_info "Tagging Docker image as latest..."
docker tag "${IMAGE_NAME}:${VERSION}" "${IMAGE_NAME}:latest"

log_info "Pushing Docker image: ${DOCKER_USERNAME}/${IMAGE_NAME}:${VERSION}"
docker push "${DOCKER_USERNAME}/${IMAGE_NAME}:${VERSION}"

log_info "Pushing Docker image: ${DOCKER_USERNAME}/${IMAGE_NAME}:latest"
docker push "${DOCKER_USERNAME}/${IMAGE_NAME}:latest"

log_success "Docker image pushed successfully!"
echo -e "${GREEN}→ ${DOCKER_USERNAME}/${IMAGE_NAME}:${VERSION} and :latest are now live on Docker Hub.${NC}"
