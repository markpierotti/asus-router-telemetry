CC ?= cc
CFLAGS ?= -O2 -Wall -Wextra -std=c11

.PHONY: all test clean
all: out/router_health

out/router_health: src/router_health.c
	mkdir -p out
	$(CC) $(CFLAGS) -o $@ $<

test: out/router_health
	./out/router_health --selftest
	python3 tests/smoke.py ./out/router_health

clean:
	rm -f out/router_health
