# XMEye Downloader

**English** | [Русский](README.ru.md)

A small Windows GUI tool for downloading recordings from **XMEye / Xiongmai** DVR, NVR and HVR boxes over the network. It uses the device's native protocol (DVRIP "Sofia", TCP port **34567**), so you don't need the vendor software, a web plugin or an HDD pulled out of the box.

Pick a date range and the channels, and it downloads every recording in that range in parallel. You can see what is downloading and how fast. Recordings can also be converted to MP4 without re-encoding.

## Features

- **Search by period and channel.** The device is queried one day at a time, so long ranges aren't cut off by the device's per-search result limit.
- **Parallel downloads.** You set the number of streams (1–32). Each stream has its own connection and takes files from a shared queue.
- **Live progress.** Each file shows its stream, percentage, size and speed. The totals line shows overall progress, speed and an ETA based on the last 20 seconds.
- **Safe to stop and restart.** Files are written as `.part` and renamed only after a complete download. Finished files are skipped on the next run.
- **Automatic retries.** A failed file is retried 3 times, then marked as failed while the rest carry on.
- **Session limits.** If the device refuses extra sessions, the surplus streams drop out and the remaining ones finish their files.
- **MP4 conversion (optional, needs ffmpeg).** The Xiongmai frame headers are stripped and the stream is remuxed with the correct codec (H.264 or H.265) and frame rate. Nothing is re-encoded.
- **Optional deletion of the raw `.h264`.** It is deleted only after the MP4 has been built and passed a size check.

## Requirements

- Windows (tested on Windows 11)
- [Python](https://www.python.org/downloads/) 3.9 or newer, with tkinter (included in the standard Windows installer)
- [ffmpeg](https://ffmpeg.org/) on `PATH`. This is optional and only needed for MP4 conversion (for example `winget install ffmpeg`).

There are no third-party Python packages.

## Usage

1. Double-click `Запуск.bat` (or run `pythonw XMEyeDownloader.pyw`).
2. Enter the device IP, port (34567 by default), user name and password, then click **Подключиться** (Connect). Channel checkboxes appear.
3. Set the period in **С / По** (From / To) as `DD.MM.YYYY HH:MM` and click **Найти записи** (Find recordings). The list shows every file with its size.
4. Choose the output folder and the number of streams in **Потоков**, then click **Скачать всё** (Download all) or **Скачать выделенное** (Download selected).

The interface is in Russian.

Settings except the password are saved to `settings.json` next to the program.

### Output layout

```
<output folder>/
  канал01/                 # channel 1
    2026-09-16/
      2026-09-16_14.00.00-15.00.00.h264
      2026-09-16_14.00.00-15.00.00.mp4
  канал02/
  ...
```

### What happens on a restart

| Already in the folder          | Action                          |
|--------------------------------|---------------------------------|
| `.mp4`                         | skipped                         |
| `.h264` without `.mp4`         | queued for MP4 conversion only  |
| `.part` (interrupted download) | downloaded again from the start |
| nothing                        | downloaded                      |

The check is by path, so keep the same output folder between runs.

## Speed

These devices limit the speed **per playback stream**: about 0.9 MB/s on the unit this was written for. The total grows almost linearly with the number of streams (8 streams gave about 6.5 MB/s). The limit on simultaneous sessions depends on the model and firmware. If you ask for too many, the extra streams drop out and a line about it appears in the log.

One file is always downloaded by one stream, because the device sends a file as a single sequential stream.

## Troubleshooting

| Message / code                     | Meaning                                                                                         |
|------------------------------------|-------------------------------------------------------------------------------------------------|
| `[203]` / `[106]`                  | wrong password or user name                                                                     |
| `[205]`                            | the device locked your IP after several wrong passwords. Wait about 30 min or power-cycle it.   |
| `регистратор не отвечает (таймаут)` (device not responding, timeout) | network problem, or the device is overloaded. The file is retried automatically. |
| MP4 not created                    | the original `.h264` is kept and opens in VLC                                                   |

Every run writes a protocol log, `dvr_debug.log`, next to the program. It contains control packets and the first bytes of each stream, and is the first thing to look at if a device behaves differently.

## Files

| File                  | Purpose                                                                    |
|-----------------------|----------------------------------------------------------------------------|
| `XMEyeDownloader.pyw` | GUI: search, download queue, worker threads, MP4 converter                 |
| `xmeye.py`            | protocol client (login, file search, download) and the private-stream demuxer |
| `Запуск.bat`          | launcher without a console window                                          |

## Protocol notes

- **Login** is message 1000 with a "Sofia" MD5-based password hash. **File search** is 1440 (`OPFileQuery`), which returns at most 64 entries per request and is paged by the last `BeginTime`. **Download** is 1424 `Claim`, then 1420 `DownloadStart`, then a stream of 1426 packets ending with an empty packet, then 1420 `DownloadStop`.
- Some firmwares answer `DownloadStart` with JSON. Others start sending video in the reply packet right away. Both cases are handled.
- The downloaded `.h264` is Xiongmai's private container, not a raw elementary stream:
  - `00 00 01 FC` is an I-frame with a 16-byte header: codec (2 = H.264, 3 = H.265), fps, width/8, height/8, timestamp, length.
  - `00 00 01 FD` is a P-frame with an 8-byte header.
  - `00 00 01 FA` and `F9` are audio and info frames.

  Despite the extension, the video is often **H.265**.

## Credits

The protocol implementation is based on [OpenIPC/python-dvr](https://github.com/OpenIPC/python-dvr) (`dvrip.py`, `NVRVideoDownloader.py`, MIT license). It was rewritten here to stream files straight to disk, report progress, support cancelling and run several downloads at once.
