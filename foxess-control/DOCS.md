# FoxESS Control

A Home Assistant custom integration for monitoring and controlling FoxESS inverter battery modes via the FoxESS Cloud API.

## What this add-on does

This add-on installs the FoxESS Control integration into your Home Assistant instance. On startup it copies the integration files into your `custom_components/` directory. You must restart Home Assistant after the first install for the integration to load.

## Installation

1. Add this repository to your Home Assistant add-on store:
   **Settings > Add-ons > Add-on Store > ⋮ > Repositories** and enter `https://github.com/nicois/foxess-control`.
2. Install **FoxESS Control** from the store.
3. Start the add-on.
4. Restart Home Assistant.

## After installation

Once Home Assistant restarts, configure the integration:

1. Go to **Settings > Devices & Services > Add Integration**.
2. Search for **FoxESS Control**.
3. Enter your FoxESS Cloud API key and inverter serial number.

For full documentation on configuration options, actions, and features, see the [README](https://github.com/nicois/foxess-control).

## Updating

When a new version is available, update the add-on in the add-on store and restart Home Assistant.
