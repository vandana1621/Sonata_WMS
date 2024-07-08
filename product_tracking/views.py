import os
import re
import time
import json
import serial
import socket
import logging
from decimal import Decimal
from datetime import datetime
import serial.tools.list_ports
from django.urls import reverse
from django.db import connection
from psycopg2 import DatabaseError
from django.contrib import messages
from .decorators import check_module_access
from django.shortcuts import redirect, render
from django.http import JsonResponse, HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.db import DatabaseError, OperationalError
from django.contrib.auth import logout
from django.views.decorators.http import require_POST

logger = logging.getLogger(__name__)
serial_number_counter = 1
serial_number = 1
serial_number_map = {}
prev_item_code = None
prev_batch = None


def custom_login(request):
    if request.method == 'POST':
        username = request.POST.get('username')
        password = request.POST.get('password')
        line_number = request.POST.get('line_number')

        with connection.cursor() as cursor:
            cursor.execute("SELECT is_valid, user_id FROM public.validate_user(%s, %s, %s)", [username, password, True])
            result = cursor.fetchone()

            if result and result[0]:
                user_id = result[1]
                cursor.execute(
                    "SELECT mm.module_name FROM user_junction_master ujm JOIN module_master mm ON ujm.module_id = mm.module_id WHERE ujm.user_id = %s",
                    [user_id])
                modules = cursor.fetchall()

                # Store user info and modules in session
                request.session['username'] = username
                request.session['user_id'] = user_id
                request.session['line_number'] = line_number
                request.session['modules'] = [module[0] for module in modules]

                if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                    return JsonResponse({'status': 'success', 'redirect_url': reverse('product_tracking:index')})
                else:
                    return redirect(reverse('product_tracking:index'))
            else:
                messages.error(request, "Invalid login details")
                return render(request, 'product_tracking/auth-signin.html', {'line_list': get_line_list()})

    return render(request, 'product_tracking/auth-signin.html', {'line_list': get_line_list()})


def get_line_list():
    """Utility function to fetch line list."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pl_id, line_no FROM pl_master")
        lines = cursor.fetchall()
        return [{'pl_id': line[0], 'line_no': line[1]} for line in lines]


def index_view(request):
    # Retrieve session data
    username = request.session.get('username', 'N/A')
    line_number = request.session.get('line_number', 'N/A')
    user_id = request.session.get('user_id', None)  # Retrieve the user_id to check if it's correctly stored
    modules = request.session.get('modules', None)

    # Log the retrieved session data
    print(f"Accessing index view")
    print(f"Session Data - Username: {username}, Line Number: {line_number}, User ID: {user_id}, Modules:{modules}")

    if not username or username == 'N/A':
        print("No username in session; redirecting to login")
        return redirect('product_tracking:login_view')

    # If session data is valid, render the index page with the session data
    return render(request, 'product_tracking/index.html', {
        'username': username,
        'line_number': line_number,
        'user_id': user_id  # Optionally pass user_id to the template if needed
    })


def production_order(request):
    line_number = request.session.get('line_number', 'N/A')
    username = request.user.username
    return render(request, 'product_tracking/production-order.html', {'username': username, 'line_number': line_number})


def label_printing(request):
    username = request.user.username
    line_number = request.session.get('line_number', 'N/A')  # Get the line number from session
    return render(request, 'product_tracking/label-printing.html', {'username': username, 'line_number': line_number})


def label_reprinting(request):
    line_number = request.session.get('line_number', 'N/A')
    username = request.user.username
    return render(request, 'product_tracking/label-reprinting.html', {'username': username, 'line_number': line_number})


def stock_verification(request):
    line_number = request.session.get('line_number', 'N/A')
    username = request.user.username
    return render(request, 'product_tracking/stock-verification-request.html',
                  {'username': username, 'line_number': line_number})


def stock_approval(request):
    line_number = request.session.get('line_number', 'N/A')
    username = request.user.username
    return render(request, 'product_tracking/stock-approval.html', {'username': username, 'line_number': line_number})


def user_logout(request):
    logout(request)
    return redirect(reverse('product_tracking:login_view'))  # Corrected from 'login' to 'login_view'


def add_user(request):
    line_number = request.session.get('line_number', 'N/A')
    username = request.session.get('username', 'N/A')
    user_id = request.session.get('user_id', None)

    if request.method == 'POST':
        username = request.POST.get('username')
        password = request.POST.get('password')
        status = request.POST.get('status', '1')  # If status is not provided, default to '1' (active)
        modules = request.POST.getlist('modules')

        # Password validation
        if not is_valid_password(password):
            error_message = 'Password must be at least 8 characters long, alphanumeric, and contain at least one special character.'
            return render(request, 'product_tracking/user-master.html',
                          {'username': username, 'line_number': line_number, 'error_message': error_message})

        created_date = datetime.now()

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT add_user_and_modules(%s, %s, %s, %s, %s, %s);",
                [username, password, status, modules, user_id, created_date])
            user_id = cursor.fetchone()[0]
        return JsonResponse({'message': 'User Added successfully'}, status=200)

    else:
        return render(request, 'product_tracking/user-master.html', {'username': username, 'line_number': line_number})


def is_valid_password(password):
    # Password should be at least 8 characters long, alphanumeric, and contain at least one special character
    if len(password) < 8:
        return False

    # Check for alphanumeric and special character
    if not re.match(r'^(?=.*[a-zA-Z])(?=.*\d)(?=.*[@$!%*?&])[A-Za-z\d@$!%*?&]+$', password):
        return False

    return True


def add_production_line(request):
    line_number = request.session.get('line_number', 'N/A')
    username = request.session.get('username', 'N/A')
    created_by = request.session.get('user_id')

    if not created_by:
        return JsonResponse({'success': False, 'message': 'Session expired, please log in again.'}, status=403)

    if request.method == 'POST':
        line_no = request.POST.get('line_no').strip()  # Clean input
        created_date = datetime.now()

        # Ensure created_by is an integer
        try:
            created_by = int(created_by)  # Convert to int, might raise ValueError if not possible
        except ValueError:
            return JsonResponse({'success': False, 'message': 'Invalid user ID'}, status=400)

        with connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM pl_master WHERE line_no = %s", [line_no])
            line_count = cursor.fetchone()[0]

            if line_count > 0:
                return JsonResponse({'success': False, 'message': 'Line already exists!'})

            try:
                cursor.execute(
                    "SELECT add_product_line(%s, %s, %s);",
                    [line_no, created_by, created_date])
                cursor.fetchone()  # Assuming function returns something
                return JsonResponse({'success': True, 'message': 'Production line added successfully'})
            except Exception as e:
                print("An unexpected error occurred:", e)
                return JsonResponse({'success': False, 'message': str(e)}, status=500)
    else:
        # Return the page with the form initially
        return render(request, 'product_tracking/production-line.html',
                      {'username': username, 'line_number': line_number})


def get_production_line(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT * FROM get_pl()")
            rows = cursor.fetchall()
            production_lines = []
            for row in rows:
                production_lines.append({
                    'line_no': row[0],
                    'created_by': row[1],
                    'created_date': row[2].strftime('%d-%m-%Y'),
                    'user_name': row[3]
                })
        return JsonResponse({'success': True, 'production_lines': production_lines})
    except Exception as e:
        print("An unexpected error occurred:", e)
        return JsonResponse({'success': False, 'message': 'An unexpected error occurred'})


def delete_production_line(request):
    if request.method == 'POST':
        line_no = request.POST.get('line_no')
        try:
            with connection.cursor() as cursor:
                cursor.callproc('delete_pl', [line_no])
                return JsonResponse({'success': True, 'message': 'Production Line deleted successfully!'})
        except Exception as e:
            print("An unexpected error occurred:", e)
            return JsonResponse(
                {'success': False, 'message': 'An unexpected error occurred while deleting the production line.'})
    else:
        return JsonResponse({'success': False, 'message': 'Invalid request method.'})


def user_list(request):
    logger = logging.getLogger(__name__)
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT * FROM getuser()")
            rows = cursor.fetchall()

        user_listing = []
        for row in rows:
            created_date_time = row[6].strftime('%d-%m-%Y')
            user_listing.append({
                'user_id': row[0],
                'user_name': row[1],
                'password': row[2],
                'status': row[3],
                'modules': row[4],
                'created_by': row[5],
                'created_date_time': created_date_time
            })
        logger.info(f'Fetched {len(user_listing)} users')
        return JsonResponse(user_listing, safe=False)
    except Exception as e:
        logger.error(f'Error fetching users: {e}')
        return HttpResponse(status=500)


@csrf_exempt
def update_user(request, user_id):
    if request.method == 'POST':
        print("Entering in function...")
        print('Received POST request to update user details')

        user_id = request.POST.get('user_id')
        user_name = request.POST.get('user_name')
        password = request.POST.get('password')
        status = request.POST.get('status') == 'Active'
        modules_json = request.POST.get('modules', '[]')
        modules = json.loads(modules_json)
        created_date_time = datetime.now()

        # Log received data
        print('User ID:', user_id)
        print('Received data:', {
            'user_id': user_id,
            'user_name': user_name,
            'password': password,
            'status': status,
            'modules': modules,
            'created_date_time': created_date_time
        })
        try:
            with connection.cursor() as cursor:
                cursor.callproc('update_user', [user_id, user_name, password, status, modules, created_date_time])
                cursor.execute("COMMIT;")
                print('User details updated successfully in the database.')
            return JsonResponse({'message': 'User details updated successfully', 'user_id': user_id})
        except Exception as e:
            return JsonResponse({'error': str(e)})
    else:
        return JsonResponse({'error': 'Invalid request method'}, status=405)


@require_POST
def delete_user(request, user_id):
    if request.method == 'POST':
        try:
            with connection.cursor() as cursor:
                cursor.callproc('deleteuser', [user_id])

            return JsonResponse({'message': 'User deleted successfully'}, status=200)
        except Exception as e:
            return JsonResponse({'error': str(e)}, status=500)
    else:
        return JsonResponse({'error': 'Invalid request method'}, status=405)


@check_module_access('Item Master')
def add_item(request):
    line_number = request.session.get('line_number', 'N/A')
    username = request.session.get('username', 'N/A')
    created_by = request.session.get('user_id')
    # Ensure you're getting the username from session

    print('created_by', created_by)

    if request.method == 'POST':
        item_code = request.POST.get('item_code')
        item_name = request.POST.get('item_name')
        item_uom = request.POST.get('item_uom')
        item_igcode = request.POST.get('item_igcode')
        status = request.POST.get('status', 'true') == 'true'  # Default to true if not provided
        # created_by = request.session.get('user_id')  # Assuming user ID is stored in session
        created_date = datetime.now()

        # Check if created_by is not None or empty
        if not created_by:
            print("Error: Created by (user ID) is missing.")
            messages.error(request, "Your session may have expired. Please login again.")
            return redirect('product_tracking:login_view')

        print("Attempting to add new item:")
        print(f"Code: {item_code}, Name: {item_name}, UOM: {item_uom}, IGCode: {item_igcode}, Status: {status}")
        print(f"Created by: {created_by}, Date: {created_date}")

        with connection.cursor() as cursor:
            try:
                cursor.execute(
                    "SELECT add_item(%s::varchar, %s::varchar, %s::varchar, %s::varchar, %s::boolean, %s::integer, %s::timestamp);",
                    [item_code, item_name, item_uom, item_igcode, status, created_by, created_date]
                )
                item_id = cursor.fetchone()[0]
                messages.success(request, f"Item added successfully, Item ID: {item_id}")
                print("Item added successfully, Item ID:", item_id)
                return redirect('product_tracking:add_item')
            except Exception as e:
                print("Failed to add item:", e)
                messages.error(request, "Failed to add item due to an error.")

    return render(request, 'product_tracking/product-master.html', {'username': username, 'line_number': line_number})


def item_list(request):
    with connection.cursor() as cursor:
        cursor.callproc("getitems")
        items = cursor.fetchall()

    # Convert the result into a list of dictionaries
    item_listing = []
    for item in items:
        created_date_time = item[7].strftime('%d-%m-%Y')
        item_listing.append({
            'item_id': item[0],
            'item_code': item[1],
            'item_name': item[2],
            'item_uom': item[3],
            'item_igcode': item[4],
            'status': item[5],
            'created_by': item[8],
            'created_date_time': created_date_time,
            'user_name': item[8]
        })

    return JsonResponse(item_listing, safe=False)


def update_item(request, item_id):
    if not request.session.get('user_id'):
        messages.error(request, "Your session may have expired. Please log in again.")
        return redirect('product_tracking:login_view')

    if request.method == 'POST':
        item_code = request.POST.get('item_code')
        item_name = request.POST.get('item_name')
        item_uom = request.POST.get('item_uom')
        item_igcode = request.POST.get('item_igcode')
        status = request.POST.get('status') == 'true'
        updated_by = request.session.get('user_id')
        updated_date = datetime.now()

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT update_item(%s::integer, %s::varchar, %s::varchar, %s::varchar, %s::varchar, %s::boolean, %s::integer, %s::timestamp);",
                [item_id, item_code, item_name, item_uom, item_igcode, status, updated_by, updated_date])

        return JsonResponse({'status': 'success'})
    else:
        with connection.cursor() as cursor:
            cursor.execute("SELECT * FROM item_master WHERE item_id = %s", [item_id])
            item = cursor.fetchone()

        if item:
            item_data = {
                'item_id': item[0],
                'item_code': item[1],
                'item_name': item[2],
                'item_uom': item[3],
                'item_igcode': item[4],
                'status': item[5],
            }
            return JsonResponse(item_data)
        else:
            return JsonResponse({'error': 'Item not found'}, status=404)


def delete_item(request, item_id):
    logger.info("Deleting item with ID: %s", item_id)  # Log the item ID being deleted
    with connection.cursor() as cursor:
        cursor.execute("SELECT delete_item(%s::integer);", [item_id])
        deleted = cursor.fetchone()[0]
        if deleted:
            logger.info("Item deleted successfully")
            return JsonResponse({'status': 'success'})
        else:
            logger.error("Failed to delete item")
            return JsonResponse({'status': 'error', 'message': 'Failed to delete item'})


def get_item_code(request):
    with connection.cursor() as cursor:
        cursor.execute("SELECT item_id, item_code, item_name FROM item_master WHERE status = TRUE")
        items = cursor.fetchall()
        item_list = [{'item_id': item[0], 'item_code': item[1], 'item_name': item[2]} for item in items]
    return JsonResponse(item_list, safe=False)


def get_item_name(request, item_id):
    with connection.cursor() as cursor:
        cursor.execute("SELECT item_id, item_code, item_name FROM item_master WHERE status = TRUE")
        items = cursor.fetchall()
        item_list = [{'item_id': item[0], 'item_code': item[1], 'item_name': item[2]} for item in items]
    return JsonResponse(item_list, safe=False)


def get_line_numbers(request):
    with connection.cursor() as cursor:
        cursor.execute("SELECT pl_id, line_no FROM pl_master")
        lines = cursor.fetchall()
        line_list = [{'pl_id': line[0], 'line_no': line[1]} for line in lines]
    return JsonResponse(line_list, safe=False)


@csrf_exempt
def create_production_order(request):
    line_number = request.session.get('line_number', 'N/A')
    username = request.session.get('username', 'N/A')
    created_by = request.session.get('user_id')  # Retrieve the user_id from the session

    # Debugging output
    print(f"Session Data - Username: {username}, User ID: {created_by}")

    if not created_by:
        print("Error: Created by (user ID) is missing.")
        messages.error(request, "Your session may have expired. Please login again.")
        return redirect(reverse('product_tracking:login_view'))

    if request.method == 'POST':
        print("Received POST data:", request.POST)

        item_code = request.POST.get('item_code')
        batch = request.POST.get('batch')
        item_mrp = request.POST.get('item_mrp')
        mfg_date = request.POST.get('mfg_date')
        exp_date = request.POST.get('exp_date')
        qty = request.POST.get('qty')
        polybag_weight = request.POST.get('polybag_weight')
        line_no = request.POST.get('line_no')
        product_name = request.POST.get('product_name')

        # Check if any of the required fields are missing
        missing_fields = [field for field in
                          ['item_code', 'batch', 'item_mrp', 'mfg_date', 'exp_date', 'qty', 'polybag_weight', 'line_no',
                           'product_name'] if not request.POST.get(field)]
        if missing_fields:
            return JsonResponse({'status': 'error', 'message': f'Missing fields: {", ".join(missing_fields)}'},
                                status=400)

        try:
            qty = int(qty)
        except ValueError:
            return JsonResponse({'status': 'error', 'message': 'Invalid quantity format.'}, status=400)

        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT item_id FROM item_master WHERE item_code = %s", [item_code])
                item = cursor.fetchone()
                if not item:
                    return JsonResponse({'status': 'error', 'message': 'Invalid item code.'}, status=400)
                item_id = item[0]

            created_date = datetime.now()

            print(
                f"Item ID: {item_id}, Batch: {batch}, MRP: {item_mrp}, MFG Date: {mfg_date}, EXP Date: {exp_date}, Qty: {qty}, Line No: {line_no}, Product Name: {product_name}")

            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT create_production_order(%s::integer, %s::character varying, %s::character varying, %s::date, %s::date, %s::integer, %s::integer, %s:: timestamp without time zone, %s::character varying, %s::character varying, %s::numeric)",
                    [
                        item_id, batch, item_mrp, mfg_date, exp_date, qty, created_by, created_date, product_name,
                        line_no, polybag_weight
                    ])

            return JsonResponse({'status': 'success', 'message': 'Production order created successfully'})

        except Exception as e:
            logger.error(f"Unhandled exception: {e}")
            return JsonResponse({'status': 'error', 'message': 'An unexpected error occurred. Please try again later.'},
                                status=500)

    return JsonResponse({'status': 'error', 'message': 'Invalid request method'}, status=405)


def get_production_order_list(request):
    with connection.cursor() as cursor:
        cursor.execute("SELECT * FROM get_polist()")
        data = cursor.fetchall()
        production_orders = []
        for row in data:
            production_orders.append({
                'production_order_number': row[0],
                'item_code': row[1],
                'product_name': row[2],
                'batch': row[3],
                'item_mrp': row[4],
                'mfg_date': row[5].strftime('%Y-%m-%d'),
                'exp_date': row[6].strftime('%Y-%m-%d'),
                'line_no': row[7],
                'qty': row[8],
                'added_by': row[9],
                'added_date': row[10].strftime('%d-%m-%Y'),
                'polybag_weight': row[11]
            })

    return JsonResponse(production_orders, safe=False)


@csrf_exempt
def update_production_order(request):
    line_number = request.session.get('line_number', 'N/A')
    username = request.session.get('username', 'N/A')
    created_by = request.session.get('user_id')  # Retrieve the user_id from the session

    if not created_by:
        messages.error(request, "Your session may have expired. Please login again.")
        return redirect(reverse('product_tracking:login_view'))

    if request.method == 'POST':
        try:
            production_order_number = request.POST.get('production_order_number')
            item_code = request.POST.get('item_code')
            batch = request.POST.get('batch')
            item_mrp = request.POST.get('item_mrp')
            mfg_date = request.POST.get('mfg_date')
            exp_date = request.POST.get('exp_date')
            qty = request.POST.get('qty')
            polybag_weight = request.POST.get('polybag_weight')
            line_no = request.POST.get('line_no')
            product_name = request.POST.get('product_name')

            if not (
                    production_order_number and item_code and batch and item_mrp and mfg_date and exp_date and qty and polybag_weight and line_no and product_name):
                logger.error('Missing fields in the request')
                return JsonResponse({'error': 'All fields are required.'}, status=400)

            # Fetch item_id based on item_code
            with connection.cursor() as cursor:
                cursor.execute("""
                    SELECT item_id FROM item_master WHERE item_code = %s
                """, [item_code])
                item = cursor.fetchone()

                if not item:
                    logger.error('Item code %s not found in item_master', item_code)
                    return JsonResponse({'error': 'Invalid item code.'}, status=400)

                item_id = item[0]

                # Ensure qty and other numeric fields are converted to integer
                qty = int(qty)
                polybag_weight = float(polybag_weight)

                cursor.execute("""
                    SELECT polybag_print_status FROM production_order WHERE production_order_number = %s
                """, [production_order_number])
                polybag_status = cursor.fetchone()

                if polybag_status and polybag_status[0]:
                    logger.error('Polybag print status is true for order %s', production_order_number)
                    return JsonResponse({'error': 'Cannot edit Production Order. Polybag print status is true.'},
                                        status=400)

                cursor.execute("""
                    SELECT update_po(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, [
                    production_order_number,
                    item_id,  # Use item_id fetched from item_master
                    batch,
                    item_mrp,
                    mfg_date,
                    exp_date,
                    qty,  # Ensure qty is cast to an integer
                    line_no,
                    product_name,
                    polybag_weight
                ])

                connection.commit()  # Ensure changes are committed

        except DatabaseError as db_error:
            logger.error('Database error: %s', db_error)
            return JsonResponse({'error': 'Database error: ' + str(db_error)}, status=500)
        except Exception as e:
            logger.error('Internal server error: %s', e)
            return JsonResponse({'error': 'Internal server error: ' + str(e)}, status=500)

        return JsonResponse({'message': 'Production order updated successfully'})

    return JsonResponse({'error': 'Invalid request method'}, status=400)


@csrf_exempt
def check_polybag_status(request):
    if request.method == 'GET':
        production_order_number = request.GET.get('production_order_number')

        try:
            with connection.cursor() as cursor:
                cursor.execute("""
                    SELECT polybag_print_status FROM production_order WHERE production_order_number = %s
                """, [production_order_number])
                polybag_status = cursor.fetchone()

                if polybag_status:
                    return JsonResponse({'polybag_status': polybag_status[0]})
                else:
                    return JsonResponse({'error': 'Production order not found'}, status=404)

        except Exception as e:
            return JsonResponse({'error': str(e)}, status=500)

    return JsonResponse({'error': 'Invalid request method'}, status=400)


def add_tolerance(request):
    line_number = request.session.get('line_number', 'N/A')
    username = request.session.get('username', 'N/A')
    created_by = request.session.get('user_id')  # Retrieve the user_id from the session

    # Debugging output
    print(f"Session Data - Username: {username}, User ID: {created_by}")

    if not created_by:
        print("Error: Created by (user ID) is missing.")
        messages.error(request, "Your session may have expired. Please login again.")
        return redirect(reverse('product_tracking:login_view'))

    if request.method == 'POST':
        lower_tolerance = request.POST.get('lower_tolerance')
        upper_tolerance = request.POST.get('upper_tolerance')
        unit = request.POST.get('unit')
        created_date = datetime.now()

        try:
            with connection.cursor() as cursor:
                # Check if the tolerance setting already exists
                cursor.execute(
                    "SELECT COUNT(*) FROM tolerance_master WHERE lower_tolerance = %s AND upper_tolerance = %s AND unit = %s",
                    [lower_tolerance, upper_tolerance, unit])
                if cursor.fetchone()[0] > 0:
                    return JsonResponse({'success': False, 'message': 'Tolerance Already Exists!'})

                # Insert the new tolerance into the database
                cursor.execute(
                    "INSERT INTO tolerance_master (lower_tolerance, upper_tolerance, created_by, created_date, status, unit) VALUES (%s, %s, %s, %s, %s, %s) RETURNING tl_id;",
                    [lower_tolerance, upper_tolerance, created_by, created_date, True, unit])
                tl_id = cursor.fetchone()[0]

            return JsonResponse({'success': True, 'tl_id': tl_id})

        except Exception as e:
            print("An unexpected error occurred:", e)
            return JsonResponse({'success': False, 'message': 'An unexpected error occurred'})
    else:
        return render(request, 'product_tracking/tolerance-master.html', {
            'username': username,
            'line_number': line_number
        })


def get_tolerance_list(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT 
                    tm.tl_id, CONCAT(tm.lower_tolerance, ' ', tm.unit) AS lower_tolerance, CONCAT(tm.upper_tolerance, ' ', tm.unit) AS upper_tolerance, um.user_name, tm.created_date, tm.status 
                FROM 
                    tolerance_master tm 
                INNER JOIN 
                    user_master um ON tm.created_by = um.user_id
            """)
            rows = cursor.fetchall()
            print("Rows:", rows)
            tolerance = []
            for row in rows:
                tolerance.append({
                    'tl_id': row[0],
                    'lower_tolerance': row[1],
                    'upper_tolerance': row[2],
                    'created_by': row[3],
                    'created_date': row[4].strftime('%d-%m-%Y'),
                    'status': row[5]
                })
            cursor.execute("SELECT COUNT(*) FROM tolerance_master")
            is_empty = cursor.fetchone()[0] == 0
        return JsonResponse({'success': True, 'tolerance': tolerance, 'is_empty': is_empty})
    except Exception as e:
        print("An unexpected error occurred:", e)
        return JsonResponse({'success': False, 'message': 'An unexpected error occurred'})


def update_tolerance(request):
    line_number = request.session.get('line_number', 'N/A')
    username = request.session.get('username', 'N/A')
    created_by = request.session.get('user_id')  # Retrieve the user_id from the session

    # Debugging output
    print(f"Session Data - Username: {username}, User ID: {created_by}")

    if not created_by:
        print("Error: Created by (user ID) is missing.")
        messages.error(request, "Your session may have expired. Please login again.")
        return redirect(reverse('product_tracking:login_view'))

    if request.method == 'POST':
        tolerance_id = request.POST.get('tolerance_id')
        new_lower_tolerance = request.POST.get('new_lower_tolerance')
        new_upper_tolerance = request.POST.get('new_upper_tolerance')
        new_unit = request.POST.get('new_unit')

        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT update_tl(%s, %s, %s, %s, %s);",
                               [tolerance_id, new_lower_tolerance, new_upper_tolerance, new_unit, created_by])
            return JsonResponse({'success': True})
        except Exception as e:
            return JsonResponse({'success': False, 'message': 'An unexpected error occurred: ' + str(e)})
    else:
        return JsonResponse({'success': False, 'message': 'Invalid request method'})


def add_mould(request):
    line_number = request.session.get('line_number', 'N/A')
    username = request.session.get('username', 'N/A')
    created_by = request.session.get('user_id')  # Retrieve the user_id from the session

    # Debugging output
    print(f"Session Data - Username: {username}, User ID: {created_by}")

    if not created_by:
        print("Error: Created by (user ID) is missing.")
        messages.error(request, "Your session may have expired. Please login again.")
        return redirect(reverse('product_tracking:login_view'))

    if request.method == 'POST':
        mould_weight = request.POST.get('mould_weight')
        mould_unit = request.POST.get('mould_unit')
        mould_weight = mould_weight
        created_date = datetime.now()
        line_no = request.POST.get('production_line')

        try:
            with connection.cursor() as cursor:
                # Check if the line number is already present in the tolerance_master table
                cursor.execute(
                    "SELECT COUNT(*) FROM mould_master WHERE line_no = %s",
                    [line_no])
                if cursor.fetchone()[0] > 0:
                    return JsonResponse({'success': False, 'message': 'Line Number Already Exists!'})

                cursor.execute(
                    "SELECT COUNT(*) FROM mould_master WHERE mould_weight = %s AND mould_unit = %s AND line_no = %s",
                    [mould_weight, mould_unit, line_no])
                mould_count = cursor.fetchone()[0]

                if mould_count > 0:
                    return JsonResponse({'success': False, 'message': 'Mould Already Exists!'})

                cursor.execute(
                    "INSERT INTO mould_master (mould_weight, created_by, created_date, status, mould_unit, line_no) VALUES (%s, %s, %s, %s, %s, %s) RETURNING mould_id;",
                    [mould_weight, created_by, created_date, True, mould_unit,
                     line_no])  # Insert unit into the database
                mould_id = cursor.fetchone()[0]

                cursor.execute("SELECT COUNT(*) FROM mould_master")
                is_empty = cursor.fetchone()[0] == 0

            return JsonResponse({'success': True, 'is_empty': is_empty})

        except Exception as e:
            print("An unexpected error occurred:", e)
            return JsonResponse({'success': False, 'message': 'An unexpected error occurred'})
    else:
        return render(request, 'product_tracking/tolerance-master.html',
                      {'username': username, 'line_number': line_number})


def get_mould_list(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT 
                    mm.mould_id, CONCAT(mm.mould_weight, ' ', mm.mould_unit) AS mould, um.user_name, mm.created_date, mm.status, mm.line_no 
                FROM 
                    mould_master mm 
                INNER JOIN 
                    user_master um ON mm.created_by = um.user_id
            """)
            rows = cursor.fetchall()
            mould = []
            for row in rows:
                mould.append({
                    'mould_id': row[0],
                    'mould_weight': row[1],
                    'created_by': row[2],
                    'created_date': row[3].strftime('%d-%m-%Y'),
                    'status': row[4],
                    'line_no': row[5]
                })
            cursor.execute("SELECT COUNT(*) FROM mould_master")
            is_empty = cursor.fetchone()[0] == 0
        return JsonResponse({'success': True, 'tolerance': mould, 'is_empty': is_empty})
    except (DatabaseError, OperationalError) as db_err:
        print("Database error occurred:", db_err)
        return JsonResponse({'success': False, 'message': 'A database error occurred'})
    except Exception as e:
        print("An unexpected error occurred:", e)
        return JsonResponse({'success': False, 'message': 'An unexpected error occurred'})


def update_mould(request):
    created_by = request.session.get('user_id')  # Retrieve the user_id from the session

    if not created_by:
        messages.error(request, "Your session may have expired. Please login again.")
        return redirect(reverse('product_tracking:login_view'))

    if request.method == 'POST':
        mould_id = request.POST.get('mould_id')
        new_mould_weight = request.POST.get('new_mould_weight')
        new_mould_unit = request.POST.get('new_mould_unit')
        new_line_no = request.POST.get('new_line_no')

        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT update_ml(%s, %s, %s, %s);",
                    [mould_id, new_mould_weight, new_mould_unit, new_line_no]
                )
            return JsonResponse({'success': True})
        except Exception as e:
            error_message = str(e)
            if 'The line number' in error_message:
                message = 'The line number already exists for another mould.'
            elif 'The weight' in error_message:
                message = 'The weight and unit combination already exists for another mould.'
            else:
                message = 'An unexpected error occurred.'

            return JsonResponse({'success': False, 'message': message})
    elif request.method == 'GET':
        if request.GET.get('action') == 'get_production_lines':
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT DISTINCT line_no FROM mould_master WHERE line_no IS NOT NULL ORDER BY line_no;")
                    lines = cursor.fetchall()
                    production_lines = [line[0] for line in lines]
                return JsonResponse({'success': True, 'production_lines': production_lines})
            except Exception as e:
                print("An unexpected error occurred:", e)
                return JsonResponse({'success': False, 'message': 'An unexpected error occurred'})
    else:
        return JsonResponse({'success': False, 'message': 'Invalid request method'})


def get_production_order_numbers_by_line(request):
    line_number = request.session.get('line_number')
    if not line_number:
        return JsonResponse({'error': 'Line number not found in session'}, status=400)

    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT production_order_number, polybag_print_status 
                FROM production_order 
                WHERE line_no = %s AND polybag_print_status = false AND sc_flag = false
                """, [line_number]
            )
            production_orders = cursor.fetchall()

            data = [
                {'production_order_no': order[0], 'polybag_print_status': order[1]}
                for order in production_orders
            ]
            return JsonResponse(data, safe=False)

    except Exception as e:
        logger.error(f"Error fetching production order numbers: {e}")
        return JsonResponse({'error': 'An error occurred while fetching production order numbers'}, status=500)


def get_product_details_by_order(request, production_order_no):
    logger.info(f"Fetching details for production order: {production_order_no}")
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                '''SELECT item_id, batch, item_mrp, "MFG_date", "EXP_date", qty 
                FROM production_order 
                WHERE production_order_number = %s AND polybag_print_status = false''',
                [production_order_no])
            row = cursor.fetchone()

            if not row:
                logger.warning(f"No data found for production order {production_order_no}")
                return JsonResponse({'error': 'Production order not found'}, status=404)

            item_id, batch, item_mrp, mfg_date, exp_date, qty = row
            cursor.execute("SELECT item_code, item_name FROM item_master WHERE item_id = %s", [item_id])
            item_row = cursor.fetchone()

            if not item_row:
                logger.warning(f"No item details found for item ID {item_id}")
                return JsonResponse({'error': 'Item details not found'}, status=404)

            item_code, item_name = item_row
            data = {
                'item_code': item_code,
                'item_name': item_name,
                'batch': batch,
                'mrp': item_mrp,
                'mfg_date': mfg_date.strftime('%Y-%m-%d'),
                'exp_date': exp_date.strftime('%Y-%m-%d'),
                'qty': qty,
            }
            return JsonResponse(data)

    except Exception as e:
        logger.error(f"Error accessing the database for production order {production_order_no}: {str(e)}")
        return JsonResponse({'error': 'An error occurred accessing the database'}, status=500)


def find_weighing_scale_port():
    ports = serial.tools.list_ports.comports()
    for port in ports:
        logging.debug(f"Checking port: {port.device} with description: {port.description}")
        if 'USB-SERIAL CH340' in port.description:
            logging.debug(f"Found port: {port.device}")
            return port.device
    return None


def read_port(ser):
    try:
        start_time = time.time()
        buffer = ""
        while True:
            if ser.in_waiting > 0:
                data = ser.read(ser.in_waiting).decode('ascii', errors='ignore')
                buffer += data
                logging.debug(f"Raw data read from port: {data}")

                matches = re.findall(r'\d+=\d*\.\d+|\d+', buffer)
                for match in matches:
                    try:
                        weight = float(match.split('=')[1])
                        logging.debug(f"Parsed weight: {weight}")
                        return weight
                    except (ValueError, IndexError) as e:
                        logging.debug(f"Invalid segment: {match}, error: {e}")
                        continue

                buffer = buffer[-1000:]

            if time.time() - start_time > 10:
                logging.error("Timeout: No valid weight data received from the weighing scale.")
                break
            time.sleep(0.1)
    except serial.SerialException as e:
        logging.error(f"SerialException: {e}")
    except Exception as e:
        logging.error(f"General error: {e}")
    return None


def get_tolerance_value():
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT lower_tolerance, upper_tolerance, unit FROM tolerance_master WHERE status = TRUE LIMIT 1;")
            result = cursor.fetchone()
            if result:
                lower_tolerance, upper_tolerance, unit = result
                return Decimal(lower_tolerance), Decimal(upper_tolerance), unit
            else:
                return None, None, None
    except Exception as e:
        logging.error(f"Error fetching tolerance values: {e}")
        return None, None, None


def get_mould_weight(request):
    line_number = request.session.get('line_number', 'N/A')
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT mould_weight FROM mould_master WHERE line_no = %s;", [line_number])
            result = cursor.fetchone()
            if result:
                return Decimal(result[0])  # mould_weight is the first element in the tuple
            else:
                return None
    except Exception as e:
        logging.error(f"Error fetching mould weight: {e}")
        return None


def get_printer_details(line_number):
    try:
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT printer_ip, port
                FROM printer_master
                WHERE production_line = %s
            """, [line_number])
            row = cursor.fetchone()
            if row:
                return row[0], int(row[1])  # Ensure port is an integer
            else:
                return None, None
    except Exception as e:
        logging.error(f"Error fetching printer details: {e}")
        return None, None


@csrf_exempt
def get_weight(request):
    try:
        if request.method != 'POST':
            return JsonResponse({'error': 'Invalid request method. Only POST is allowed.'}, status=400)

        po_no = request.POST.get('production_order_no')
        if not po_no:
            return JsonResponse({'error': 'Production Order Number is required.'}, status=400)

        port = find_weighing_scale_port()
        if port:
            logging.debug(f"Attempting to open serial port: {port}")
            try:
                ser = serial.Serial(port, baudrate=9600, timeout=0.5)
                logging.debug(f"Serial port {port} opened successfully")
            except serial.SerialException as e:
                logging.error(f"SerialException: {e}")
                return JsonResponse({'error': f"Error connecting to serial port: {e}"}, status=500)

            weight = read_port(ser)
            ser.close()

            if weight is not None:
                logging.info(f"Weight received from the scale: {weight} kg")

                lower_tolerance, upper_tolerance, unit = get_tolerance_value()
                if lower_tolerance is None or upper_tolerance is None or unit is None:
                    return JsonResponse({'error': "Tolerance values not found."}, status=500)

                if unit.lower() == 'gm':
                    lower_tolerance /= Decimal(1000)
                    upper_tolerance /= Decimal(1000)

                # Fetch polybag weight from the production_order table
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT polybag_weight FROM production_order WHERE production_order_number = %s
                        """, [po_no]
                    )
                    result = cursor.fetchone()
                    if result is None:
                        return JsonResponse({'error': 'Production Order not found.'}, status=404)
                    polybag_weight = Decimal(result[0])

                mould_weight = get_mould_weight(request)
                if mould_weight is None:
                    return JsonResponse({'error': "Mould weight not found."}, status=500)
                mould_weight = Decimal(mould_weight)
                total_weight = polybag_weight + mould_weight

                tolerance_lower = total_weight - lower_tolerance
                tolerance_upper = total_weight + upper_tolerance

                logging.info(f"Tolerance range: {tolerance_lower} kg to {tolerance_upper} kg")

                if tolerance_lower <= Decimal(weight) <= tolerance_upper:
                    logging.info(
                        f"Weight {weight} kg is within the tolerance range ({tolerance_lower} kg - {tolerance_upper} kg)")
                    return JsonResponse({'weight': weight})
                else:
                    error_message = f"Weight {weight} kg is out of tolerance range ({tolerance_lower} kg - {tolerance_upper} kg)."
                    logging.error(error_message)
                    return JsonResponse({'error': error_message})
            else:
                error_message = "Could not read weight from the weighing scale."
                logging.error(error_message)
                return JsonResponse({'error': error_message})
        else:
            error_message = "Could not find weighing scale port."
            logging.error(error_message)
            return JsonResponse({'error': error_message})
    except serial.SerialException as e:
        error_message = f"Error connecting to serial port: {e}"
        logging.error(error_message)
        return JsonResponse({'error': error_message})


@csrf_exempt
def generate_prn(request):
    try:
        # Session data retrieval
        line_number = request.session.get('line_number')
        username = request.session.get('username')
        created_by = request.session.get('user_id')

        if not created_by:
            logger.error("Error: Created by (user ID) is missing.")
            return JsonResponse({'status': 'error', 'message': 'User ID is missing. Please login again.'}, status=401)

        # Request handling for POST method
        if request.method == 'POST':
            logger.info("Received POST data: %s", request.POST)
            batch = request.POST.get('batch')
            item_code = request.POST.get('item_code')
            item_name = request.POST.get('item_name')
            mrp = request.POST.get('mrp')
            exp_date = request.POST.get('exp_date')
            weight = Decimal(request.POST.get('weight'))
            qty = Decimal(request.POST.get('qty'))
            po_no = request.POST.get('production_order_no')

            if not po_no:
                logger.error("Production Order Number is required.")
                return JsonResponse({'status': 'error', 'message': 'Production Order Number is required.'}, status=400)

            current_month_year = datetime.now().strftime("%m%y")

            # Retrieve tolerance values and unit
            lower_tolerance, upper_tolerance, unit = get_tolerance_value()
            if lower_tolerance is None or upper_tolerance is None or unit is None:
                logger.error("Tolerance values not found.")
                return JsonResponse({'status': 'error', 'message': 'Tolerance values not found.'}, status=500)

            lower_tolerance = Decimal(lower_tolerance)
            upper_tolerance = Decimal(upper_tolerance)

            if unit.lower() == 'gm':
                lower_tolerance /= Decimal(1000)
                upper_tolerance /= Decimal(1000)

            # Fetch polybag weight from the production_order table
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT polybag_weight FROM production_order WHERE production_order_number = %s
                        """, [po_no]
                    )
                    result = cursor.fetchone()
                    if result is None:
                        logger.error("Production Order not found.")
                        return JsonResponse({'status': 'error', 'message': 'Production Order not found.'}, status=404)
                    polybag_weight = Decimal(result[0])
            except Exception as e:
                logger.error("Error fetching polybag weight: %s", e)
                return JsonResponse({'status': 'error', 'message': 'Error fetching polybag weight.'}, status=500)

            # Calculate total weight with mould weight
            try:
                mould_weight = get_mould_weight(request)
                if mould_weight is None:
                    logger.error("Mould weight not found.")
                    return JsonResponse({'status': 'error', 'message': 'Mould weight not found.'}, status=500)
                mould_weight = Decimal(mould_weight)
                total_weight = polybag_weight + mould_weight
            except Exception as e:
                logger.error("Error calculating mould weight: %s", e)
                return JsonResponse({'status': 'error', 'message': 'Error calculating mould weight.'}, status=500)

            # Calculate tolerance range
            tolerance_lower = total_weight - lower_tolerance
            tolerance_upper = total_weight + upper_tolerance

            # Check if weight is within tolerance range
            if tolerance_lower <= weight <= tolerance_upper:
                directory = os.path.join('D:', 'WMS_sample_updated', 'WMS-PRN')
                if not os.path.exists(directory):
                    os.makedirs(directory)

                # Retrieve line number from session
                line_number = request.session.get('line_number')
                if not line_number:
                    logger.error("Line number not found in session.")
                    return JsonResponse({'status': 'error', 'message': 'Line number not found in session.'}, status=500)

                # Retrieve printer details
                try:
                    ip, port = get_printer_details(line_number)
                    if ip is None or port is None:
                        logger.error("Printer details not found.")
                        return JsonResponse({'status': 'error', 'message': 'Printer details not found.'}, status=500)
                except Exception as e:
                    logger.error("Error retrieving printer details: %s", e)
                    return JsonResponse({'status': 'error', 'message': 'Error retrieving printer details.'}, status=500)

                successful_prints = 0
                qr_codes = []

                try:
                    # Socket communication with printer
                    logger.info("Attempting to connect to printer at %s:%s", ip, port)
                    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                        s.settimeout(5)  # Set a timeout for socket operations (adjust as needed)
                        try:
                            s.connect((ip, port))
                            logger.info("Connected to printer at %s:%s", ip, port)
                        except socket.error as e:
                            logger.error("Failed to connect to printer: %s", e)
                            return JsonResponse({'status': 'error',
                                                 'message': 'Failed to connect to printer. Please check if the printer is on and connected.'},
                                                status=500)

                        # Retrieve the last serial number printed for this PO, item, and batch
                        try:
                            with connection.cursor() as cursor:
                                cursor.execute(
                                    """
                                    SELECT MAX(CAST(SUBSTRING(qr_code FROM '.{4}$') AS INTEGER))
                                    FROM label_printing
                                    WHERE po_no = %s AND item_code = %s AND batch = %s
                                    """,
                                    [po_no, item_code, batch]
                                )
                                last_serial = cursor.fetchone()[0]
                                if last_serial is None:
                                    last_serial = 0
                        except Exception as e:
                            logger.error("Error retrieving last serial number: %s", e)
                            return JsonResponse({'status': 'error', 'message': 'Error retrieving last serial number.'},
                                                status=500)

                        serial_number_counter = last_serial + 1

                        qr_code = f"P{item_code}{current_month_year}{batch}{serial_number_counter:04d}"
                        qr_codes.append(qr_code)

                        # Generate PRN content
                        prn_content = f"""Seagull:2.1:DP
                        INPUT OFF
                        VERBOFF
                        INPUT ON
                        SYSVAR(48) = 0
                        ERROR 15,"FONT NOT FOUND"
                        ERROR 18,"DISK FULL"
                        ERROR 26,"PARAMETER TOO LARGE"
                        ERROR 27,"PARAMETER TOO SMALL"
                        ERROR 37,"CUTTER DEVICE NOT FOUND"
                        ERROR 1003,"FIELD OUT OF LABEL"
                        SYSVAR(35)=0
                        OPEN "tmp:setup.sys" FOR OUTPUT AS #1
                        PRINT#1,"Printing,Media,Print Area,Media Margin (X),0"
                        PRINT#1,"Printing,Media,Clip Default,On"
                        CLOSE #1
                        SETUP "tmp:setup.sys"
                        KILL "tmp:setup.sys"
                        CLIP ON
                        CLIP BARCODE ON
                        LBLCOND 3,2
                        CLL
                        OPTIMIZE "BATCH" ON
                        PP35,384:AN7
                        NASC 8
                        FT "Andale Mono Bold"
                        FONTSIZE 12
                        FONTSLANT 0
                        PT "Batch"
                        PP229,384:PT "{batch}"
                        PP32,349:PT "Item code"
                        PP222,349:PT "{item_code}"
                        PP35,306:PT "EXP.Date"
                        PP229,306:PT "{exp_date}"
                        PP38,269:PT "MRP"
                        PP232,269:PT "{mrp}"
                        PP39,232:PT "SN#"
                        PP233,232:PT "{serial_number_counter:04d}"
                        PP142,171:BARSET "QRCODE",1,1,7,2,1
                        PB "{qr_code}"
                        LAYOUT RUN ""
                        PF
                        PRINT KEY OFF"""

                        file_path = os.path.join(directory, '50X50 SONATA POLY BAG.PRN')
                        with open(file_path, 'w') as file:
                            file.write(prn_content)

                        with open(file_path, 'rb') as file:
                            s.sendall(file.read())

                        successful_prints += 1

                except socket.timeout as e:
                    logger.error("Socket operation timed out: %s", e)
                    return JsonResponse({'status': 'error',
                                         'message': 'Socket operation timed out. Please check the printer connection.'},
                                        status=500)

                except socket.error as e:
                    logger.error("Failed to send data to printer: %s", e)
                    return JsonResponse({'status': 'error',
                                         'message': 'Failed to send data to printer. Please check the printer connection.'},
                                        status=500)

                except Exception as e:
                    logger.error("Error during printer communication: %s", e)
                    return JsonResponse({'status': 'error',
                                         'message': 'Error during printer communication. Please check the printer connection.'},
                                        status=500)

                # Update database with printed labels
                if successful_prints > 0:
                    printed_by = request.session.get('user_id')
                    printed_date = datetime.now()

                    try:
                        with connection.cursor() as cursor:
                            cursor.execute(
                                """
                                UPDATE production_order
                                SET total_polybags = %s
                                WHERE production_order_number = %s
                                """,
                                [int(qty / total_weight), po_no]
                            )

                            cursor.execute(
                                """
                                INSERT INTO label_printing (po_no, batch, item_code, quantity, qr_code, printed_by, printed_date)
                                VALUES (%s, %s, %s, %s, %s, %s, %s)
                                """,
                                [po_no, batch, item_code, 1, qr_code, printed_by, printed_date]
                            )

                            # Fetch total quantity of labels printed
                            cursor.execute(
                                """
                                SELECT COUNT(*) FROM label_printing WHERE po_no = %s
                                """, [po_no]
                            )
                            total_labels = cursor.fetchone()[0]

                            # Update no_of_labels and polybag_print_status if all labels are printed
                            cursor.execute(
                                """
                                UPDATE production_order
                                SET no_of_labels = %s, polybag_print_status = CASE WHEN %s >= %s THEN true ELSE false END
                                WHERE production_order_number = %s
                                """,
                                [total_labels, total_labels, int(qty / total_weight), po_no]
                            )

                        return JsonResponse({'status': 'success',
                                             'message': f'PRN file sent to printer successfully. {successful_prints} prints generated.',
                                             'num_prints': successful_prints})

                    except Exception as e:
                        logger.error("Error updating database: %s", e)
                        return JsonResponse({'status': 'error', 'message': 'Error updating database.'}, status=500)

            else:
                logger.error(
                    f"Weight {weight} kg is out of tolerance range ({tolerance_lower} kg - {tolerance_upper} kg).")
                return JsonResponse({'status': 'error',
                                     'message': f'Weight {weight} kg is out of tolerance range ({tolerance_lower} kg - {tolerance_upper} kg).'})

        else:
            logger.error("Invalid request method.")
            return JsonResponse({'status': 'error', 'message': 'Invalid request method.'}, status=400)

    except Exception as e:
        logger.error("Unhandled exception: %s", e)
        return JsonResponse({'status': 'error', 'message': 'An unexpected error occurred. Please try again later.'},
                            status=500)


def handle_request(request):
    if request.method == 'POST':
        return generate_prn(request)
    elif request.method == 'GET':
        return get_weight(request)
    else:
        return JsonResponse({'error': 'Unsupported HTTP method'}, status=405)


def get_batch_data(request):
    scan_product = request.GET.get('scan_product', None)
    label_type = request.GET.get('label_type', None)  # Default to None if not provided
    qc_flag = request.GET.get('qc_flag', None)  # Default to None if not provided

    if not scan_product:
        return JsonResponse({'error': 'No valid parameter provided'}, status=400)

    print(f"Received input: {scan_product}, Label Type: {label_type}, QC Flag: {qc_flag}")  # Debug

    query_qr = "SELECT * FROM label_printing WHERE qr_code = %s"
    query_item_batch = "SELECT * FROM label_printing WHERE item_code = %s AND batch = %s"

    if label_type:
        query_qr += " AND label_type = %s"
        query_item_batch += " AND label_type = %s"

    if qc_flag is not None:
        query_qr += " AND qc_flag = %s"
        query_item_batch += " AND qc_flag = %s"

    with connection.cursor() as cursor:
        if label_type and qc_flag is not None:
            cursor.execute(query_qr, [scan_product, label_type, qc_flag])
        elif label_type:
            cursor.execute(query_qr, [scan_product, label_type])
        elif qc_flag is not None:
            cursor.execute(query_qr, [scan_product, qc_flag])
        else:
            cursor.execute(query_qr, [scan_product])
        batch_data_qr = cursor.fetchall()
        print(f"QR Code Query Result: {batch_data_qr}")  # Debug

    if batch_data_qr:
        batch_data = batch_data_qr
    else:
        item_code = scan_product[:9]
        batch = scan_product[9:]
        print(f"Trying as Item Code: {item_code}, Batch: {batch}")  # Debug
        with connection.cursor() as cursor:
            if label_type and qc_flag is not None:
                cursor.execute(query_item_batch, [item_code, batch, label_type, qc_flag])
            elif label_type:
                cursor.execute(query_item_batch, [item_code, batch, label_type])
            elif qc_flag is not None:
                cursor.execute(query_item_batch, [item_code, batch, qc_flag])
            else:
                cursor.execute(query_item_batch, [item_code, batch])
            batch_data = cursor.fetchall()
            print(f"Item Code + Batch Query Result: {batch_data}")  # Debug

    data = []
    for row in batch_data:
        data.append({
            'label_id': row[0],
            'po_no': row[1],
            'batch': row[2],
            'item_code': row[3],
            'quantity': row[4],
            'qr_code': row[5]
        })

    if data:
        return JsonResponse({'batch_data': data})
    else:
        return JsonResponse({'batch_data': []})


@csrf_exempt
def reprint_label(request):
    if request.method == 'POST':
        qr_code = request.POST.get('qr_code')
        item_code = request.POST.get('item_code')
        item_batch = request.POST.get('item_batch')
        remark = request.POST.get('remark')
        reprinting = "Reprint"
        rework = "Rework"

        print(
            f"Received POST data - QR Code: {qr_code}, Item Code: {item_code}, Item Batch: {item_batch}, Remark: {remark}")

        if not qr_code or not item_code or not item_batch:
            return JsonResponse({'status': 'error', 'message': 'Missing parameters.'}, status=400)

        try:
            # Fetch additional details from production_order table
            with connection.cursor() as cursor:
                cursor.execute("""
                    SELECT item_mrp, "EXP_date", product_name, production_order_number
                    FROM production_order
                    WHERE item_id = (SELECT item_id FROM item_master WHERE item_code = %s) AND batch = %s
                """, [item_code, item_batch])
                row1 = cursor.fetchone()

            # Fetch label_id from label_printing table
            with connection.cursor() as cursor:
                cursor.execute("""
                    SELECT label_id
                    FROM label_printing
                    WHERE qr_code = %s AND item_code = %s AND batch = %s
                """, [qr_code, item_code, item_batch])
                row = cursor.fetchone()

            if row:
                label_id = row[0]  # Fetch label_id from the query result

                # Assuming you have logic to fetch item_mrp, exp_date, etc.
                item_mrp, exp_date, product_name, production_order_number = row1

                current_month_year = datetime.now().strftime("%m%y")
                serial_no = qr_code[-4:]

                reprinted_by = request.session.get('user_id')

                # Get the current user's line number from the session
                line_number = request.session.get('line_number')
                if not line_number:
                    return JsonResponse({'status': 'error', 'message': 'Line number not found in session.'}, status=400)

                # Get printer details for the current user's line number
                ip, port = get_printer_details(line_number)
                if ip is None or port is None:
                    return JsonResponse({'status': 'error', 'message': 'Printer details not found.'}, status=500)

                if remark:
                    try:
                        # Generate label content
                        prn_content = f"""Seagull:2.1:DP
INPUT OFF
VERBOFF
INPUT ON
SYSVAR(48) = 0
ERROR 15,"FONT NOT FOUND"
ERROR 18,"DISK FULL"
ERROR 26,"PARAMETER TOO LARGE"
ERROR 27,"PARAMETER TOO SMALL"
ERROR 37,"CUTTER DEVICE NOT FOUND"
ERROR 1003,"FIELD OUT OF LABEL"
SYSVAR(35)=0
OPEN "tmp:setup.sys" FOR OUTPUT AS #1
PRINT#1,"Printing,Media,Print Area,Media Margin (X),0"
PRINT#1,"Printing,Media,Clip Default,On"
CLOSE #1
SETUP "tmp:setup.sys"
KILL "tmp:setup.sys"
CLIP ON
CLIP BARCODE ON
LBLCOND 3,2
CLL
OPTIMIZE "BATCH" ON
PP35,384:AN7
NASC 8
FT "Andale Mono Bold"
FONTSIZE 12
FONTSLANT 0
PT "Batch"
PP229,384:PT "{item_batch}"
PP32,349:PT "Item code"
PP222,349:PT "{item_code}"
PP35,306:PT "EXP.Date"
PP229,306:PT "{exp_date}"
PP38,269:PT "MRP"
PP232,269:PT "{item_mrp}"
PP39,232:PT "SN#"
PP233,232:PT "{serial_no}"
PP142,171:BARSET "QRCODE",1,1,7,2,1
PB "{qr_code}"
LAYOUT RUN ""
PF
PRINT KEY OFF"""

                        # Save label content to a temporary file
                        file_path = os.path.join('D:\\WMS_sample_updated', '50X50 SONATA POLY BAG.PRN')
                        with open(file_path, 'w') as file:
                            file.write(prn_content)

                        # Connect to printer and send label file
                        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                            s.connect((ip, port))
                            with open(file_path, 'rb') as file:
                                s.sendall(file.read())

                        # After successful printing, insert into label_reprinting table
                        with connection.cursor() as cursor:
                            cursor.execute("""
                                INSERT INTO label_reprinting (lp_id, qr_code, reprinting_status, reprinted_by, reprinted_date, remark, type)
                                VALUES (%s, %s, %s, %s, %s, %s, %s)
                            """, [label_id, qr_code, True, reprinted_by, datetime.now(), remark, reprinting])

                        return JsonResponse({'status': 'success', 'message': 'Label reprinted successfully.'})
                    except Exception as e:
                        logging.error(f"Error printing label: {e}")
                        return JsonResponse({'status': 'error', 'message': 'Error printing label.'}, status=500)
                else:
                    try:
                        # Generate label content
                        prn_content = f"""Seagull:2.1:DP
                    INPUT OFF
                    VERBOFF
                    INPUT ON
                    SYSVAR(48) = 0
                    ERROR 15,"FONT NOT FOUND"
                    ERROR 18,"DISK FULL"
                    ERROR 26,"PARAMETER TOO LARGE"
                    ERROR 27,"PARAMETER TOO SMALL"
                    ERROR 37,"CUTTER DEVICE NOT FOUND"
                    ERROR 1003,"FIELD OUT OF LABEL"
                    SYSVAR(35)=0
                    OPEN "tmp:setup.sys" FOR OUTPUT AS #1
                    PRINT#1,"Printing,Media,Print Area,Media Margin (X),0"
                    PRINT#1,"Printing,Media,Clip Default,On"
                    CLOSE #1
                    SETUP "tmp:setup.sys"
                    KILL "tmp:setup.sys"
                    CLIP ON
                    CLIP BARCODE ON
                    LBLCOND 3,2
                    CLL
                    OPTIMIZE "BATCH" ON
                    PP35,384:AN7
                    NASC 8
                    FT "Andale Mono Bold"
                    FONTSIZE 12
                    FONTSLANT 0
                    PT "Batch"
                    PP229,384:PT "{item_batch}"
                    PP32,349:PT "Item code"
                    PP222,349:PT "{item_code}"
                    PP35,306:PT "EXP.Date"
                    PP229,306:PT "{exp_date}"
                    PP38,269:PT "MRP"
                    PP232,269:PT "{item_mrp}"
                    PP39,232:PT "SN#"
                    PP233,232:PT "{serial_no}"
                    PP142,171:BARSET "QRCODE",1,1,7,2,1
                    PB "{qr_code}"
                    LAYOUT RUN ""
                    PF
                    PRINT KEY OFF"""

                        # Save label content to a temporary file
                        file_path = os.path.join('D:\\WMS_sample_updated', '50X50 SONATA POLY BAG.PRN')
                        with open(file_path, 'w') as file:
                            file.write(prn_content)

                        # Connect to printer and send label file
                        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                            s.connect((ip, port))
                            with open(file_path, 'rb') as file:
                                s.sendall(file.read())

                        # After successful printing, insert into label_reprinting table
                        with connection.cursor() as cursor:
                            cursor.execute("""
                                INSERT INTO label_reprinting (lp_id, qr_code, reprinting_status, reprinted_by, reprinted_date, type)
                                VALUES (%s, %s, %s, %s, %s, %s)
                            """, [label_id, qr_code, True, reprinted_by, datetime.now(), rework])

                        return JsonResponse({'status': 'success', 'message': 'Label reprinted successfully.'})
                    except Exception as e:
                        logging.error(f"Error printing label: {e}")
                        return JsonResponse({'status': 'error', 'message': 'Error printing label.'}, status=500)
            else:
                return JsonResponse({'status': 'error', 'message': 'No matching record found in label_printing table.'},
                                    status=400)

        except Exception as e:
            logging.error(f"Unexpected error: {e}")
            return JsonResponse({'status': 'error', 'message': 'Unexpected error occurred.'}, status=500)

    else:
        return JsonResponse({'status': 'error', 'message': 'Invalid request method.'}, status=405)


def add_printer(request):
    line_number = request.session.get('line_number', 'N/A')
    username = request.session.get('username', 'N/A')
    created_by = request.session.get('user_id')  # Retrieve the user_id from the session

    # Debugging output
    print(f"Session Data - Username: {username}, User ID: {created_by}")

    if not created_by:
        print("Error: Created by (user ID) is missing.")
        messages.error(request, "Your session may have expired. Please login again.")
        return redirect(reverse('product_tracking:login_view'))

    if request.method == 'POST':
        printer_ip = request.POST.get('printer_ip')
        port_no = request.POST.get('port_no')
        production_line = request.POST.get('line_no')
        # created_by = request.user.id
        created_date = datetime.now()
        status = True

        # Insert data into printer_master table
        with connection.cursor() as cursor:
            cursor.execute("""
                INSERT INTO printer_master (printer_ip, port, production_line, created_by, created_date, status)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, [printer_ip, port_no, production_line, created_by, created_date, status])

        return redirect('product_tracking:add_printer')  # Redirect after POST

    else:
        line_number = request.session.get('line_number', 'N/A')
        username = request.session.get('username', 'N/A')

        # Fetch production lines from the database using raw SQL
        with connection.cursor() as cursor:
            cursor.execute("SELECT line_no FROM pl_master")
            production_lines = [row[0] for row in cursor.fetchall()]

        return render(request, 'product_tracking/printer-master.html', {
            'username': username,
            'line_number': line_number,
            'production_lines': production_lines,
        })


def get_printer_list(request):
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT pm.printer_id, pm.printer_ip, pm.port, pm.production_line, um.user_name, pm.created_date, pm.status
            FROM printer_master pm
            JOIN user_master um ON pm.created_by = um.user_id
        """)
        printers = cursor.fetchall()

    printer_list = []
    for printer in printers:
        printer_list.append({
            'printer_id': printer[0],
            'printer_ip': printer[1],
            'port': printer[2],
            'production_line': printer[3],
            'created_by': printer[4],  # username
            'created_date': printer[5].strftime('%d-%m-%Y'),
            'status': 'Active' if printer[6] else 'Inactive'
        })

    return JsonResponse({'printer_list': printer_list})


def get_production_lines(request):
    if request.method == 'GET':
        with connection.cursor() as cursor:
            cursor.execute("SELECT line_no FROM pl_master")
            production_lines = [row[0] for row in cursor.fetchall()]
        return JsonResponse({'production_lines': production_lines})
    return JsonResponse({'error': 'Invalid request'}, status=400)


@csrf_exempt
def update_printer(request):
    if request.method == 'POST':
        printer_id = request.POST.get('printer_id')
        printer_ip = request.POST.get('printer_ip')
        port_no = request.POST.get('port_no')
        production_line = request.POST.get('line_no')
        status = True if request.POST.get('status') == 'true' else False

        # Update the printer in the database
        with connection.cursor() as cursor:
            cursor.execute("""
                UPDATE printer_master
                SET printer_ip = %s, port = %s, production_line = %s, status = %s
                WHERE printer_id = %s
            """, [printer_ip, port_no, production_line, status, printer_id])

        return JsonResponse({'success': True})
    return JsonResponse({'error': 'Invalid request'}, status=400)


@csrf_exempt
def delete_printer(request):
    if request.method == 'POST':
        printer_id = request.POST.get('printer_id')

        # Delete the printer from the database
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM printer_master WHERE printer_id = %s", [printer_id])

        return JsonResponse({'success': True})
    return JsonResponse({'error': 'Invalid request'}, status=400)


@csrf_exempt
def quality_control(request):
    line_number = request.session.get('line_number', 'N/A')
    username = request.session.get('username', 'N/A')
    created_by = request.session.get('user_id')
    # Ensure you're getting the username from session

    print('created_by', created_by)
    return render(request, 'product_tracking/quality-control.html', {'username': username, 'line_number': line_number})


def get_batch_data_for_qc(request):
    scan_product = request.GET.get('scan_product', None)

    if not scan_product:
        return JsonResponse({'error': 'No valid parameter provided'}, status=400)

    print(f"Received input: {scan_product}")  # Debug

    # Try as qr_code
    query_qr = "SELECT * FROM label_printing WHERE qr_code = %s"
    query_item_batch = "SELECT * FROM label_printing WHERE item_code = %s AND batch = %s AND label_type='p'"

    with connection.cursor() as cursor:
        cursor.execute(query_qr, [scan_product])
        batch_data_qr = cursor.fetchall()
        print(f"QR Code Query Result: {batch_data_qr}")  # Debug

    if batch_data_qr:
        batch_data = batch_data_qr
    else:
        # If no result, try as item_code + batch
        item_code = scan_product[:9]
        batch = scan_product[9:]
        print(f"Trying as Item Code: {item_code}, Batch: {batch}")  # Debug
        with connection.cursor() as cursor:
            cursor.execute(query_item_batch, [item_code, batch])
            batch_data = cursor.fetchall()
            print(f"Item Code + Batch Query Result: {batch_data}")  # Debug

    # Prepare data in JSON format
    data = []
    for row in batch_data:
        data.append({
            'label_id': row[0],
            'po_no': row[1],
            'batch': row[2],
            'item_code': row[3],
            'quantity': row[4],
            'qr_code': row[5],
            'qc_flag': row[8],
            'qc_remark': row[9]
        })

    if data:
        return JsonResponse({'batch_data': data})
    else:
        return JsonResponse({'batch_data': []})


@csrf_exempt
def approve_qc(request):
    line_number = request.session.get('line_number', 'N/A')
    username = request.session.get('username', 'N/A')
    created_by = request.session.get('user_id')
    if request.method == 'POST' and request.headers.get('x-requested-with') == 'XMLHttpRequest':
        qr_code = request.POST.get('qr_code')

        # Update database with QC approval details
        with connection.cursor() as cursor:
            try:
                # Example update query
                update_query = """
                    UPDATE label_printing
                    SET qc_flag = true,
                        qc_remark = null,
                        qc_date = %s,
                        qc_by = %s
                    WHERE qr_code = %s
                """
                cursor.execute(update_query, [datetime.now(), created_by, qr_code])
                return JsonResponse({'success': 'QC Approved successfully'})
            except Exception as e:
                return JsonResponse({'error': str(e)}, status=500)
    else:
        return JsonResponse({'error': 'Invalid request method or not AJAX'}, status=400)


@csrf_exempt
def reject_qc(request):
    line_number = request.session.get('line_number', 'N/A')
    username = request.session.get('username', 'N/A')
    created_by = request.session.get('user_id')
    if request.method == 'POST' and request.headers.get('x-requested-with') == 'XMLHttpRequest':
        qr_code = request.POST.get('qr_code')
        remark = request.POST.get('remark')

        # Update database with QC rejection details
        with connection.cursor() as cursor:
            try:
                # Example update query
                update_query = """
                    UPDATE label_printing
                    SET qc_flag = false,
                        qc_remark = %s,
                        qc_date = %s,
                        qc_by = %s
                    WHERE qr_code = %s
                """
                cursor.execute(update_query, [remark, datetime.now(), created_by, qr_code])
                return JsonResponse({'success': 'QC Rejected successfully'})
            except Exception as e:
                return JsonResponse({'error': str(e)}, status=500)
    else:
        return JsonResponse({'error': 'Invalid request method or not AJAX'}, status=400)


# @csrf_exempt
# def generate_carton(request):
#     global serial_number
#
#     print("Entering in a Function....")
#
#     if request.method == 'POST':
#         try:
#             qr_code = request.POST.get('qr_code')
#             print("QR Code :", qr_code)
#             carton_type = request.POST.get('carton_type')
#             print("Carton Type :", carton_type)
#
#             if carton_type == 'single':
#                 with connection.cursor() as cursor:
#                     cursor.execute(
#                         """
#                         SELECT po_no, batch, item_code, quantity
#                         FROM label_printing
#                         WHERE qr_code = %s AND qc_flag = TRUE
#                         """, [qr_code]
#                     )
#                     result = cursor.fetchone()
#                     print("Result:", result)
#
#                     if not result:
#                         return JsonResponse({'status': 'error', 'message': 'This Polybag Has Not Been Cleared By QC'},
#                                             status=400)
#
#                     po_no, batch, item_code, quantity = result
#
#                     cursor.execute(
#                         """
#                         SELECT "EXP_date", item_mrp
#                         FROM production_order
#                         WHERE production_order_number = %s
#                         """, [po_no]
#                     )
#                     production_order_result = cursor.fetchone()
#
#                     if not production_order_result:
#                         return JsonResponse({'status': 'error', 'message': 'Production order details not found.'},
#                                             status=500)
#
#                     exp_date, item_mrp = production_order_result
#                     print("Production Order Result:", production_order_result)
#
#             current_month_year = datetime.now().strftime("%m%y")
#             new_qr_code = f"C{item_code}{current_month_year}{batch}{serial_number:04d}"
#
#             print("Current Month Year:", current_month_year)
#             print("new qr code:", new_qr_code)
#
#             line_number = request.session.get('line_number')
#             if not line_number:
#                 return JsonResponse({'status': 'error', 'message': 'Line number not found in session.'}, status=500)
#             print("Line Number:", line_number)
#
#             ip, port = get_printer_details(line_number)
#             if ip is None or port is None:
#                 return JsonResponse({'status': 'error', 'message': 'Printer details not found.'}, status=500)
#
#             print("IOP & Port:", ip, port)
#
#             try:
#                 with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
#                     s.connect((ip, port))
#
#                     # PRN content
#                     prn_content = f'''Seagull:2.1:DP
#                     INPUT OFF
#                     VERBOFF
#                     INPUT ON
#                     SYSVAR(48) = 0
#                     ERROR 15,"FONT NOT FOUND"
#                     ERROR 18,"DISK FULL"
#                     ERROR 26,"PARAMETER TOO LARGE"
#                     ERROR 27,"PARAMETER TOO SMALL"
#                     ERROR 37,"CUTTER DEVICE NOT FOUND"
#                     ERROR 1003,"FIELD OUT OF LABEL"
#                     SYSVAR(35)=0
#                     OPEN "tmp:setup.sys" FOR OUTPUT AS #1
#                     PRINT#1,"Printing,Media,Print Area,Media Margin (X),0"
#                     PRINT#1,"Printing,Media,Clip Default,On"
#                     CLOSE #1
#                     SETUP "tmp:setup.sys"
#                     KILL "tmp:setup.sys"
#                     CLIP ON
#                     CLIP BARCODE ON
#                     LBLCOND 3,2
#                     CLL
#                     OPTIMIZE "BATCH" ON
#                     PP51,383:AN7
#                     NASC 8
#                     FT "Andale Mono Bold"
#                     FONTSIZE 12
#                     FONTSLANT 0
#                     PT "Batch"
#                     PP239,383:PT "{batch}"
#                     PP47,346:PT "Item code"
#                     PP229,346:PT "{item_code}"
#                     PP51,305:PT "EXP.Date"
#                     PP239,305:PT "{exp_date}"
#                     PP53,268:PT "MRP"
#                     PP241,268:PT "{item_mrp}"
#                     PP54,230:PT "SN#"
#                     PP242,230:PT "{serial_number}"
#                     PP155,171:BARSET "QRCODE",1,1,7,2,1
#                     PB "{new_qr_code}"
#                     PP437,381:PT "Batch"
#                     PP625,381:PT "{batch}"
#                     PP434,344:PT "Item code"
#                     PP616,344:PT "{item_code}"
#                     PP437,303:PT "EXP.Date"
#                     PP625,303:PT "{exp_date}"
#                     PP439,266:PT "MRP"
#                     PP628,266:PT "{item_mrp}"
#                     PP440,228:PT "SN#"
#                     PP629,228:PT "{serial_number}"
#                     PP541,169:BARSET "QRCODE",1,1,7,2,1
#                     PB "{new_qr_code}"
#                     LAYOUT RUN ""
#                     PF
#                     PRINT KEY OFF
#                     '''
#
#                     print("PRN :", prn_content)
#
#                     directory = os.path.join('D:', 'WMS_sample_updated', 'WMS-PRN')
#
#                     if not os.path.exists(directory):
#                         os.makedirs(directory)
#
#                     file_path = os.path.join(directory, '4X2 Sonata Cartoon.prn')
#                     try:
#                         with open(file_path, 'w') as file:
#                             file.write(prn_content)
#                         print("File written successfully:", file_path)
#                     except Exception as e:
#                         print(f"Error writing file: {e}")
#                         return JsonResponse({'status': 'error', 'message': f"Error writing file: {e}"}, status=500)
#
#                     try:
#                         with open(file_path, 'rb') as file:
#                             s.sendall(file.read())
#                         print("File sent to printer successfully")
#                     except Exception as e:
#                         print(f"Error sending file to printer: {e}")
#                         return JsonResponse({'status': 'error', 'message': f"Error sending file to printer: {e}"},
#                                             status=500)
#
#                     serial_number = 1
#                     printed_by = request.session.get('user_id', None)
#                     printed_date = datetime.now()
#
#                     with connection.cursor() as cursor:
#                         try:
#                             cursor.execute(
#                                 """
#                                 INSERT INTO label_printing (po_no, batch, item_code, quantity, qr_code, printed_by, printed_date, qc_flag, label_type)
#                                 VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE, 'C')
#                                 """,
#                                 [po_no, batch, item_code, quantity, new_qr_code, printed_by, printed_date]
#                             )
#
#                             return JsonResponse(
#                                 {'status': 'success',
#                                  'message': 'PRN file sent to printer successfully.',
#                                  'qr_code': new_qr_code})
#
#                         except Exception as e:
#                             print(f"Database insert error: {e}")
#                             return JsonResponse({'status': 'error', 'message': f"Database insert error: {e}"},
#                                                 status=500)
#
#             except Exception as e:
#                 print(f"Printer connection error: {e}")
#                 return JsonResponse({'status': 'error', 'message': f"Printer connection error: {e}"}, status=500)
#
#         except Exception as e:
#             print(f"Processing error: {e}")
#             return JsonResponse({'status': 'error', 'message': f"Processing error: {e}"}, status=500)
#
#     else:
#         print("Invalid request method")
#         return JsonResponse({'error': 'Invalid request method.'}, status=400)


@csrf_exempt
def generate_carton(request):
    global serial_number_map

    print("Entering in a Function....")

    if request.method == 'POST':
        try:
            qr_code = request.POST.get('qr_code')
            print("QR Code:", qr_code)
            carton_type = request.POST.get('carton_type')
            print("Carton Type:", carton_type)

            if carton_type == 'single':
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT po_no, batch, item_code, quantity
                        FROM label_printing
                        WHERE qr_code = %s AND qc_flag = TRUE
                        """, [qr_code]
                    )
                    result = cursor.fetchone()
                    print("Result:", result)

                    if not result:
                        return JsonResponse({'status': 'error', 'message': 'This Polybag Has Not Been Cleared By QC'},
                                            status=400)

                    po_no, batch, item_code, quantity = result

                    cursor.execute(
                        """
                        SELECT "EXP_date", item_mrp
                        FROM production_order
                        WHERE production_order_number = %s
                        """, [po_no]
                    )
                    production_order_result = cursor.fetchone()

                    if not production_order_result:
                        return JsonResponse({'status': 'error', 'message': 'Production order details not found.'},
                                            status=500)

                    exp_date, item_mrp = production_order_result
                    print("Production Order Result:", production_order_result)

                    cursor.execute(
                        """
                        SELECT qr_code
                        FROM label_printing
                        WHERE mapped_polybag_qr = %s AND label_type = 'C'
                        """, [qr_code]
                    )
                    existing_carton = cursor.fetchone()

                    if existing_carton:
                        print("Existing Carton:", existing_carton)
                        return JsonResponse(
                            {'status': 'error', 'message': 'Carton Label for this polybag has already been generated.'},
                            status=400)

            current_month_year = datetime.now().strftime("%m%y")
            key = f"{item_code}{batch}"
            if key in serial_number_map:
                serial_number_map[key] += 1
            else:
                serial_number_map[key] = 1

            new_qr_code = f"C{item_code}{current_month_year}{batch}{serial_number_map[key]:04d}"

            print("Current Month Year:", current_month_year)
            print("New QR Code:", new_qr_code)

            line_number = request.session.get('line_number')
            if not line_number:
                return JsonResponse({'status': 'error', 'message': 'Line number not found in session.'}, status=500)
            print("Line Number:", line_number)

            ip, port = get_printer_details(line_number)
            if ip is None or port is None:
                return JsonResponse({'status': 'error', 'message': 'Printer details not found.'}, status=500)

            print("IP & Port:", ip, port)

            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.connect((ip, port))

                    # PRN content
                    prn_content = f'''Seagull:2.1:DP
                    INPUT OFF
                    VERBOFF
                    INPUT ON
                    SYSVAR(48) = 0
                    ERROR 15,"FONT NOT FOUND"
                    ERROR 18,"DISK FULL"
                    ERROR 26,"PARAMETER TOO LARGE"
                    ERROR 27,"PARAMETER TOO SMALL"
                    ERROR 37,"CUTTER DEVICE NOT FOUND"
                    ERROR 1003,"FIELD OUT OF LABEL"
                    SYSVAR(35)=0
                    OPEN "tmp:setup.sys" FOR OUTPUT AS #1
                    PRINT#1,"Printing,Media,Print Area,Media Margin (X),0"
                    PRINT#1,"Printing,Media,Clip Default,On"
                    CLOSE #1
                    SETUP "tmp:setup.sys"
                    KILL "tmp:setup.sys"
                    CLIP ON
                    CLIP BARCODE ON
                    LBLCOND 3,2
                    CLL
                    OPTIMIZE "BATCH" ON
                    PP51,383:AN7
                    NASC 8
                    FT "Andale Mono Bold"
                    FONTSIZE 12
                    FONTSLANT 0
                    PT "Batch"
                    PP239,383:PT "{batch}"
                    PP47,346:PT "Item code"
                    PP229,346:PT "{item_code}"
                    PP51,305:PT "EXP.Date"
                    PP239,305:PT "{exp_date}"
                    PP53,268:PT "MRP"
                    PP241,268:PT "{item_mrp}"
                    PP54,230:PT "SN#"
                    PP242,230:PT "{serial_number_map[key]}"
                    PP155,171:BARSET "QRCODE",1,1,7,2,1
                    PB "{new_qr_code}"
                    LAYOUT RUN ""
                    PF
                    PRINT KEY OFF
                    '''

                    print("PRN:", prn_content)

                    directory = os.path.join('D:', 'WMS_sample_updated', 'WMS-PRN')

                    if not os.path.exists(directory):
                        os.makedirs(directory)

                    file_path = os.path.join(directory, '4X2 Sonata Carton.prn')
                    try:
                        with open(file_path, 'w') as file:
                            file.write(prn_content)
                        print("File written successfully:", file_path)
                    except Exception as e:
                        print(f"Error writing file: {e}")
                        return JsonResponse({'status': 'error', 'message': f"Error writing file: {e}"}, status=500)

                    try:
                        with open(file_path, 'rb') as file:
                            s.sendall(file.read())
                        print("File sent to printer successfully")
                    except Exception as e:
                        print(f"Error sending file to printer: {e}")
                        return JsonResponse({'status': 'error', 'message': f"Error sending file to printer: {e}"},
                                            status=500)

                    printed_by = request.session.get('user_id', None)
                    printed_date = datetime.now()

                    with connection.cursor() as cursor:
                        try:
                            cursor.execute(
                                """
                                INSERT INTO label_printing (po_no, batch, item_code, quantity, qr_code, printed_by, printed_date, qc_flag, label_type, mapped_polybag_qr)
                                VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE, 'C', %s)
                                """,
                                [po_no, batch, item_code, quantity, new_qr_code, printed_by, printed_date, qr_code]
                            )

                            return JsonResponse(
                                {'status': 'success',
                                 'message': 'PRN file sent to printer successfully.',
                                 'qr_code': new_qr_code})

                        except Exception as e:
                            print(f"Database insert error: {e}")
                            return JsonResponse({'status': 'error', 'message': f"Database insert error: {e}"},
                                                status=500)

            except Exception as e:
                print(f"Printer connection error: {e}")
                return JsonResponse({'status': 'error', 'message': f"Printer connection error: {e}"}, status=500)

        except Exception as e:
            print(f"Processing error: {e}")
            return JsonResponse({'status': 'error', 'message': f"Processing error: {e}"}, status=500)

    else:
        print("Invalid request method")
        return JsonResponse({'error': 'Invalid request method.'}, status=400)


def get_single_carton_list(request):
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT qr_code, item_code, batch, printed_by, printed_date
            FROM label_printing
            WHERE label_type = 'C'
            """
        )
        carton_labels = cursor.fetchall()

        # Fetch user details for the printed_by field
        cursor.execute(
            """
            SELECT user_id, user_name
            FROM user_master
            """
        )
        users = cursor.fetchall()
        user_dict = {user[0]: user[1] for user in users}

    carton_labels_data = [
        {
            'qr_code': label[0],
            'item_code': label[1],
            'batch': label[2],
            'printed_by': user_dict.get(label[3], 'Unknown'),
            'printed_date': label[4].strftime("%d-%m-%Y"),
        }
        for label in carton_labels
    ]

    return JsonResponse(carton_labels_data, safe=False)


def short_closure(request):
    username = request.session.get('username', 'N/A')
    line_number = request.session.get('line_number', 'N/A')

    return render(request, 'product_tracking/short_closure.html')


def get_production_order_numbers(request):
    line_number = request.session.get('line_number', 'N/A')

    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT production_order_number 
            FROM production_order 
            WHERE polybag_print_status = false AND sc_flag = false
            AND line_no = %s
        """, [line_number])
        production_order_numbers = [row[0] for row in cursor.fetchall()]

    return JsonResponse({'production_order_numbers': production_order_numbers})


def get_po_details_for_sc(request):
    production_order_number = request.GET.get('production_order_number')

    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT total_polybags, no_of_labels 
            FROM production_order 
            WHERE production_order_number = %s
        """, [production_order_number])
        result = cursor.fetchone()

    if result:
        total_polybags, no_of_labels = result
        total_polybags = total_polybags or 0
        no_of_labels = no_of_labels or 0
        remaining_labels = max(total_polybags - no_of_labels, 0)  # Ensure no negative values
        data = {
            'total_polybags': total_polybags,
            'no_of_labels': no_of_labels,
            'remaining_labels': remaining_labels
        }
    else:
        data = {
            'total_polybags': 0,
            'no_of_labels': 0,
            'remaining_labels': 0
        }

    return JsonResponse(data)


@csrf_exempt
def save_remark(request):
    if request.method == "POST":
        production_order_number = request.POST.get('production_order_number')
        remark = request.POST.get('remark')

        with connection.cursor() as cursor:
            # Update the production order with the remark and set sc_flag to true
            cursor.execute("""
                UPDATE production_order
                SET sc_remark = %s, sc_flag = true
                WHERE production_order_number = %s
            """, [remark, production_order_number])

            # Delete rows from the label_printing table
            cursor.execute("""
                DELETE FROM label_printing
                WHERE po_no = %s
            """, [production_order_number])

        return JsonResponse({'success': True})

    return JsonResponse({'success': False})
