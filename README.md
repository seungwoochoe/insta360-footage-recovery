# Insta360 footage recovery

Case-specific reference scripts for reconstructing fragmented Insta360 MOV/MP4
files from residual exFAT FAT chains after PhotoRec has found the file starts.

Read the accompanying case study: [Recovering missing footage from an
Insta360
camera](https://www.seungwoochoe.com/blog/recovering-missing-footage-from-an-insta360-camera/).

These scripts were written for one 512 GB card. They are not a universal repair
tool and should not be run blindly against another card. Review the source,
confirm the exFAT partition start, and inspect the generated CSV files before
extracting anything.

## Safety

- Stop using the affected card.
- Never write recovered files to the source card.
- Verify raw-device identifiers by capacity and partition layout.
- The scripts open the source only with `rb`. Outputs are first written as
  `.partial` files and atomically renamed after their expected length is
  complete.
- If the card reports read errors or disconnects, stop and consider a
  professional recovery service.

## Requirements

- macOS or another Unix-like system with raw block-device access
- Python 3.9 or later
- FFmpeg and FFprobe for validation
- PhotoRec output retaining names such as `f123456_ftyp.mov`
- A recovery destination on another physical disk

TestDisk and PhotoRec can be installed on macOS with:

```sh
brew install testdisk
```

## 1. Identify and unmount the card

A card in a Mac's built-in SD slot may be classified as internal:

```sh
diskutil list internal physical
```

For an external reader, try:

```sh
diskutil list external physical
```

Confirm the identifier, capacity, and partition layout, then unmount the whole
card without ejecting it:

```sh
diskutil unmountDisk /dev/diskN
```

Use the whole-card raw device, `/dev/rdiskN`, in the commands below. Raw-device
access normally requires `sudo` on macOS.

## 2. Run PhotoRec first

Run PhotoRec against the same raw device and save its output to another disk:

```sh
sudo photorec /dev/rdiskN
```

Select the exFAT partition, ensure MOV/MP4 recovery is enabled in **File Opt**,
choose **Other** as the filesystem type, scan the **Whole** partition, and save
to another physical disk. Keep the original `recup_dir.*` filenames and
`report.xml`.

## 3. Inventory the remaining exFAT records

Replace every path below. The example partition start sector, `65536`, matched
the card from this case study and is not universal. TestDisk can display the
partition start.

```sh
sudo python3 exfat_recover.py \
  --source /dev/rdiskN \
  --partition-sector 65536 \
  --photorec /Volumes/Recovery/Recovered_PhotoRec \
  --csv inventory.csv \
  --json inventory.json \
  --date-from 20260727 \
  --date-to 20260730 \
  --include-lrv
```

This reads surviving exFAT directory entries and maps PhotoRec starts that
still retain filenames.

## 4. Map PhotoRec starts to residual FAT chains

```sh
sudo python3 scan_orphan_chains.py \
  --source /dev/rdiskN \
  --partition-sector 65536 \
  --photorec /Volumes/Recovery/Recovered_PhotoRec \
  --inventory-json inventory.json \
  --timezone Asia/Seoul \
  --csv chain-scan.csv \
  --json chain-scan.json
```

This maps the sector number in each PhotoRec filename back to an exFAT start
cluster, follows the residual FAT chain, parses MP4 atom boundaries, and reads
QuickTime creation timestamps. Review `chain-scan.csv` before continuing.

## 5. Extract the selected dates

```sh
sudo python3 extract_target_recoveries.py \
  --source /dev/rdiskN \
  --partition-sector 65536 \
  --scan-json chain-scan.json \
  --inventory-json inventory.json \
  --destination /Volumes/Recovery/Repaired \
  --date-from 2026-07-27 \
  --date-to 2026-07-30 \
  --timezone Asia/Seoul
```

Existing files with a different size are not overwritten.

## 6. Validate decoded frames

```sh
python3 validate_recoveries.py --root /Volumes/Recovery/Repaired
```

The validator probes every container and asks FFmpeg to decode frames near the
beginning, midpoint, and end. Results and frame hashes are written to
`validation.csv`.

## Limitations

- Useful residual FAT chains must still exist.
- PhotoRec sector numbers must correspond to the selected exFAT partition as
  they did in this case.
- Files without a complete `moov` index cannot be dated or honestly rebuilt by
  these scripts.
- The parser targets the GO Ultra layout observed here: `ftyp`, `wide`, `mdat`,
  `moov`, and optional `inst`.
- A parseable container is not necessarily valid footage. Decode real frames
  and review the generated CSV files.
