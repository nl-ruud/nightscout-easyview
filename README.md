# Nightscout EasyView Uploader

This project provides a **Python-based uploader** that retrieves CGM data from the **Medtrum EasyView** 
API and pushes it to a **Nightscout** instance. It is intended for personal use by people who rely on 
Medtrum devices but want their data available in Nightscout.

## Features
- Login to Medtrum EasyView
- Fetch CGM readings from your Medtrum device
- Upload data automatically to a Nightscout server
- Simple configuration and extensible codebase

## Requirements
- Python 3.11 or higher
- `requests` and `pyyaml` dependencies
- A Medtrum Easyview **follower** account that follows exactly 1 user
- A working Nightscout instance

## Usage
1. Clone this repository
2. Install dependencies
3. Configure your credentials for Medtrum EasyView and Nightscout (see below).
4. Run the uploader:
   ```bash
   python uploader.py
   ```

## Configuration

Credentials are read from `~/.nightscout_easyview/secrets.yaml`.

The file should have the following structure:

```yaml
easyview:
    username: <e-mail for follower account>
    password: <password of follower acount>

nightscout:
    url: https:/your.nightscout.instance.here
    secret: <your nightscout API secret>
```

## Notes
- This is **not** an official Medtrum or Nightscout project.
- Use at your own risk.
- Contributions are welcome.


## Acknowledgements
Special thanks to [bruderjakob12](https://gist.github.com/bruderjakob12/b492e5c0b32e421d6fc9ee6f86d5f2dd)
for his work in reverse engineering parts of the Medtrum EasyView API. 
His research provided valuable insights that made this uploader possible.

