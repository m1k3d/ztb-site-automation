#!/usr/bin/env python3
"""CSV command-line adapter for the reusable deployment engine."""

import argparse
from automation_config import Settings
from deployment_engine import DeploymentEngine
from input_validation import validate_csv
from run_report import reserve_report, save_report


def main(argv=None):
    parser = argparse.ArgumentParser(description="Validate, preview, or deploy selected ZTB sites")
    parser.add_argument("--csv", default="sites.csv", help="Site CSV; VLAN paths are relative to this file")
    parser.add_argument("--env-file", default=".env", help="Credential file (process environment takes precedence)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--validate-only", action="store_true", help="Offline input checks only; no credentials or network needed")
    mode.add_argument("--dry-run", action="store_true", help="Validate and resolve API references; do not create deployment resources")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--report-dir", default="out/runs", help="Directory for short text and JSON run reports")
    args = parser.parse_args(argv)
    validation = validate_csv(args.csv)
    if validation.issues:
        for issue in validation.issues:
            print(f"ERR : {issue}")
        print("Validation failed. No deployment resources were changed. Correct the errors and validate again.")
        return 1
    print(f"Validated {len(validation.sites)} selected site(s), {sum(len(s.vlans) for s in validation.sites)} VLAN(s).")
    if args.validate_only or not validation.sites:
        if not validation.sites:
            print("Nothing selected: set post=1 for sites you intend to deploy.")
        return 0
    try:
        config = Settings.load(args.env_file)
        errors = config.errors(require_zpa=any(s.row.get("appc_provision") == "1" for s in validation.sites))
        if errors:
            for error in errors:
                print(f"ERR : configuration: {error}")
            return 1
        engine = DeploymentEngine(config, debug=args.debug)
        try:
            report_path = reserve_report(args.report_dir)
            print(f"Run report: {report_path}")
            result = engine.run(validation, dry_run=args.dry_run)
            save_report(report_path, result, dry_run=args.dry_run)
            return result.exit_code
        finally:
            engine.client.close()
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"ERR : {exc}")
        return 1
    except KeyboardInterrupt:
        print("Interrupted. Inspect existing resources before rerunning; automatic resume is not supported.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
