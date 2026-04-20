#!/bin/bash
set -e

cd ~ || exit

echo "::group::Install Bench"
pip install --upgrade pip
pip install frappe-bench
echo "::endgroup::"

echo "::group::Init Bench"
bench init frappe-bench \
  --frappe-branch version-15 \
  --skip-assets \
  --python "$(which python)"

cd frappe-bench || exit
echo "::endgroup::"

echo "::group::Get Apps"

# Core Apps
bench get-app erpnext --branch version-15
bench get-app hrms --branch version-15
bench get-app payments --branch version-15
bench get-app lending --branch version-15
# bench get-app wiki --branch version-14
# bench get-app helpdesk --branch version-14

# Custom / Private Apps
bench get-app https://github.com/resilient-tech/india-compliance.git --branch version-15
bench get-app https://github.com/Gurukrupa-Export/jewellery-erpnext.git --branch develop_aerele
bench get-app https://github.com/Gurukrupa-Export/gke_customization.git --branch develop_aerele
bench get-app https://github.com/Gurukrupa-Export/gurukrupa_biometric.git --branch master
bench get-app https://github.com/Gurukrupa-Export/gurukrupa_customizations.git --branch main

echo "::endgroup::"

echo "::group::Start Redis"
redis-server --daemonize yes
sleep 5

bench set-config -g redis_cache redis://127.0.0.1:6379
bench set-config -g redis_queue redis://127.0.0.1:6379
bench set-config -g redis_socketio redis://127.0.0.1:6379
echo "::endgroup::"

echo "::group::Create Site"

bench new-site test_site \
  --admin-password admin \
  --db-root-password travis \
  --mariadb-root-username root \
  --no-mariadb-socket

echo "::endgroup::"

echo "::group::Server Script Enable"
bench set-config -g server_script_enabled true
bench --site test_site set-config server_script_enabled 1
echo "::endgroup::"

echo "::group::Install Apps on Site"

bench --site test_site install-app erpnext
bench --site test_site install-app hrms
bench --site test_site install-app payments
bench --site test_site install-app india_compliance
bench --site test_site install-app lending
# bench --site test_site install-app wiki
# bench --site test_site install-app helpdesk

bench --site test_site install-app jewellery_erpnext
bench --site test_site install-app gke_customization
bench --site test_site install-app gurukrupa_biometric
bench --site test_site install-app gurukrupa_customizations

echo "::endgroup::"

echo "::group::Migrate"
bench --site test_site migrate --skip-search-index
echo "::endgroup::"
