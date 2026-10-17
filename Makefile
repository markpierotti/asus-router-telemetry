CC ?= cc
CFLAGS ?= -O2 -Wall -Wextra -std=c11

.PHONY: all test test-reporter test-collector test-deployment clean
all: out/router_health

out/router_health: src/router_health.c
	mkdir -p out
	$(CC) $(CFLAGS) -o $@ $<

test: test-reporter test-collector test-deployment

test-reporter: out/router_health
	./out/router_health --selftest
	python3 tests/smoke.py ./out/router_health
	bash tests/fixture_tests.sh ./out/router_health
	bash tests/reporter_edge_tests.sh ./out/router_health
	python3 tests/reporter_audit_regressions.py ./out/router_health

test-collector:
	cd collector && PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test_*.py' -v

test-deployment:
	PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s deploy/slim/tests -p 'test_*.py' -v

clean:
	rm -f out/router_health
