.PHONY: flash-promicro flash-xiao

WEST ?= west

PROMICRO_BOARD := promicro_nrf52840/nrf52840/uf2
PROMICRO_BUILD_DIR := build.promicro

XIAO_BOARD := xiao_ble/nrf52840/sense
XIAO_BUILD_DIR := build.xiao

flash-promicro:
	$(WEST) build -p auto -b $(PROMICRO_BOARD) -d $(PROMICRO_BUILD_DIR) .
	$(WEST) flash -d $(PROMICRO_BUILD_DIR)

flash-xiao:
	$(WEST) build -p auto -b $(XIAO_BOARD) -d $(XIAO_BUILD_DIR) .
	$(WEST) flash -d $(XIAO_BUILD_DIR)
