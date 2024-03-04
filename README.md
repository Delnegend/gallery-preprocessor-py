# Gallery Preprocessor

## Process
From a directory (called `foo`), creating 2 archives:

- `foo_archive` dir contains
    - original PNGs, JPGs, GIFs -> lossless JXL
    - other files -> copy
-> `foo.7z`

- `foo_dist` dir contains
    - original PNGs, JPGs, WEBPs -> upscaled AVIF images
    - videos -> x264
-> `foo.zip`

## Reprocess
From a processed directory (called `bar`), re-creating the `.zip` one