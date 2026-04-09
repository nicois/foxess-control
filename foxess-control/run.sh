#!/usr/bin/with-contenv bashio
# shellcheck shell=bash

DEST="/config/custom_components/foxess_control"

bashio::log.info "Installing FoxESS Control integration..."

mkdir -p "$DEST"
rm -rf "$DEST"/*
cp -a /opt/foxess_control/* "$DEST"/

bashio::log.info "FoxESS Control installed to ${DEST}"
bashio::log.info "Home Assistant must be restarted for changes to take effect."
