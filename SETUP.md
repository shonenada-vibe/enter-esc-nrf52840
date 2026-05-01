## Setup west and zephyr

```sh
$ pip install west
$ mkdir -p /path/to/zephyr-home
$ cd /path/to/zephyr-home
$ west init .
$ west update
```

Export environment variables:

```sh
$ west zephyr-export
$ west packages pip --install
$ west sdk install
```

## Build external app

If your app lives outside the west workspace, use the workspace root to build it:

```sh
$ cd /path/to/zephyr-workspace
$ export ZEPHYR_BASE=/path/to/zephyr-workspace/zephyr
$ west build -p always -b promicro_nrf52840/nrf52840/uf2 \
    -s /path/to/enter-esc-nrf52840 \
    -d /path/to/enter-esc-nrf52840/build
```


## Log

```
$ ls /dev/cu.usbmodem*
$ screen /dev/cu.usbmodemXXXX
```
