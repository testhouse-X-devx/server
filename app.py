from flask import Flask, jsonify, request
from flask_cors import CORS
from config import Config
import stripe
import requests
from functools import lru_cache
from datetime import datetime, timedelta
import requests
import os
from datetime import datetime
from sqlalchemy import create_engine, Column, Integer, String, DateTime, ForeignKey, Enum, Boolean
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.orm import Session
from enum import Enum as PyEnum
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from email_service import EmailService



# trial plan creation + integration , bundles / plan creation & listing.  


app = Flask(__name__)
app.config.from_object(Config)

CORS(app, resources={
    r"/*": {
        "origins": ["http://localhost:5173", "http://127.0.0.1:5173"],
        "methods": ["GET", "POST", "OPTIONS"],
        "allow_headers": ["Content-Type", "Authorization"]
    }
})


Base = declarative_base()
engine = create_engine(app.config['SQLALCHEMY_DATABASE_URI'])
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
email_service = EmailService()

class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True)
    email = Column(String(255), unique=True)
    role = Column(String(50), default='admin')  # Keep this enum as it's working
    
    # Admin-specific columns
    stripe_customer_id = Column(String(255), unique=True, nullable=True)
    stripe_subscription_id = Column(String(255), unique=True, nullable=True)
    max_users = Column(Integer, default=0)
    has_used_trial = Column(Boolean, default=False)
    trial_end_date = Column(DateTime, nullable=True)
    
     # New fields for subscription cancellation
    is_blocked = Column(Boolean, default=False)
    benefits_end_date = Column(DateTime, nullable=True)
    # Common columns for both admin and team members
    current_credits = Column(Integer, default=0)
    current_scans = Column(Integer, default=0)
    
    # Team member creation tracking
    created_by = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class Transaction(Base):
    __tablename__ = 'transactions'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'))
    primary_type = Column(String(50), nullable=False)  # 'credit' or 'scan' or 'generation'
    source_type = Column(String(50), nullable=False)   # 'subscription', 'top_up', or 'trial' 
    transaction_type = Column(String(50), nullable=False)  # 'received', 'used', or 'reset'
    value = Column(Integer)
    subscription_id = Column(String(255))
    payment_id = Column(String(255))
    description = Column(String(255))
    created_at = Column(DateTime, default=datetime.utcnow)
# Create tables
def init_db():
    try:
        Base.metadata.create_all(bind=engine)
        print("Database tables created successfully")
    except Exception as e:
        print(f"Error creating tables: {e}")

# Call this after app initialization
init_db()
# Stripe API Key
stripe.api_key = os.getenv('STRIPE_SECRET_KEY', 'sk_test_51QfxxdEf1mVEQwuO8oaLn5HrxduggqJc6iWANlc8G6CaFmcNzkvJ7wKXQLhRQRotSVGdRXUhaRkvWU0OMEhYHxho002cRkFu4R')
EXCHANGE_RATE_API_KEY = os.getenv('EXCHANGE_RATE_API_KEY', '9493897f152ce55047ac6a08')

class PricingService:
    def __init__(self):
        self.api_key = '9493897f152ce55047ac6a08'
        self.base_url = 'https://v6.exchangerate-api.com/v6'
        self.cache_duration = timedelta(hours=1)
        self._last_update = None
        self._rates = None
    
    def _should_update_cache(self):
        """Check if the cached rates should be updated"""
        return (
            self._last_update is None or 
            datetime.now() - self._last_update > self.cache_duration
        )
    
    def get_exchange_rates(self):
        if self._should_update_cache():
            try:
                url = f"{self.base_url}/{self.api_key}/latest/GBP"
                print(f"Fetching rates from: {url}")
                
                response = requests.get(url)
                print(f"Response status: {response.status_code}")
                
                if response.status_code == 200:
                    data = response.json()
                    print(f"API Response: {data}")
                    if 'conversion_rates' in data:
                        self._rates = data['conversion_rates']
                        self._last_update = datetime.now()
                    else:
                        print("No conversion_rates in response")
                else:
                    print(f"Failed to fetch rates: {response.text}")
            except Exception as e:
                print(f"Exception in get_exchange_rates: {str(e)}")
                return None
                
        return self._rates
    
    def convert_price(self, amount, from_currency, to_currency):
        print(f"Converting {amount} from {from_currency} to {to_currency}")
        
        # If same currency, return original amount
        if from_currency.upper() == to_currency.upper():
            return amount
            
        rates = self.get_exchange_rates()
        if not rates:
            print("No rates available")
            return amount
            
        try:
            # Direct conversion from GBP to target currency
            if from_currency.upper() == 'GBP':
                conversion_rate = rates.get(to_currency.upper())
                if conversion_rate:
                    converted_amount = amount * conversion_rate
                    print(f"Rate: {conversion_rate}")
                    print(f"Result: {converted_amount}")
                    return round(converted_amount, 2)
            
            print(f"No conversion rate found for {from_currency} to {to_currency}")
            return amount
        except Exception as e:
            print(f"Error in convert_price: {str(e)}")
            return amount
pricing_service = PricingService()

def get_currency_for_country(country_code):
    currency_map = {
        
        'US': 'USD',
        'GB': 'GBP',
        
    }
    result = currency_map.get(country_code.upper(), 'USD')
    print(f"Currency for {country_code}: {result}")  # Debug print
    return result


def get_payment_methods_for_country(country_code):
    """Return available payment methods based on country"""
    country_specific_methods = {
        
        'US': ['card', 'us_bank_account'],
        'GB': ['card', 'bacs_debit'],
    
        # Add more country-specific mappings as needed
    }
    
    # Default to just card if no specific methods are defined
    return country_specific_methods.get(country_code.upper(), ['card'])


@app.route('/api/products', methods=['GET'])
def get_products():
    try:
        selected_option = request.args.get('option', '')
        products = stripe.Product.list(active=True)
        product_data = []

        for product in products.data:
            # Get base price first
            prices = stripe.Price.list(product=product.id)
            if not prices.data:
                continue
                
            base_price = prices.data[0]  # Using first price as base price
            base_unit_amount = base_price.unit_amount / 200  # Base price is for 200 credits

            # Get all price options from metadata
            credit_options = []
            for key in product.metadata:
                if key.startswith('option-') and key.replace('option-', '').isdigit():
                    try:
                        credits = int(product.metadata[key])
                        credit_options.append({
                            "key": key,
                            "credits": credits
                        })
                    except (ValueError, TypeError):
                        print(f"Warning: Invalid value for {key}: {product.metadata[key]}")
            
            credit_options = sorted(credit_options, key=lambda x: x['credits'])
            
            # Calculate price for each credit option
            price_data = []
            for option in credit_options:
                price_entry = {
                    "option": option['key'],
                    "credits": option['credits'],
                    "amount": (base_unit_amount * option['credits']) / 100,  # Convert to currency units
                    "price_id": base_price.id
                }
                price_data.append(price_entry)

            product_entry = {
                "id": product.id,
                "name": product.name,
                "description": product.description,
                "validity_in_days": int(product.metadata.get('validity_in_days', 90)),
                "credit_options": credit_options,
                "prices": price_data
            }

            if selected_option:
                matching_prices = [p for p in price_data if p['option'] == selected_option]
                if matching_prices:
                    product_entry['selected_price'] = matching_prices[0]

            product_data.append(product_entry)

        return jsonify({
            "products": product_data,
            "filters": {
                "selected_option": selected_option or "all"
            }
        }), 200

    except Exception as e:
        print(f"Error in get_products: {str(e)}")
        return jsonify({"error": str(e)}), 500
def format_currency(amount, currency):
    """Helper function to format currency amounts"""
    symbols = {'usd': '$', 'gbp': '£', 'eur': '€'}
    symbol = symbols.get(currency.lower(), '')
    return f"{symbol}{amount:.2f}"
def get_user_by_email(email: str, db: Session):
   return db.query(User).filter(User.email == email).first()

@app.route('/api/create-checkout-session', methods=['POST'])
def create_checkout_session():
   db = SessionLocal()
   try:
       data = request.get_json()
       price_id = data.get('priceId')
       email = data.get('email')
       country_code = data.get('countryCode', 'US')
    #    product_type = data.get('productType', 'subscription_plan')

       if not email:
           return jsonify({'error': 'Email is required'}), 400

       # Get price and product details to check type
       price = stripe.Price.retrieve(price_id)
       product = stripe.Product.retrieve(price.product)
       product_type = product.metadata.get('type')

       user = get_user_by_email(email, db)
       if not user:
           user = User(email=email)
           db.add(user)

       # Validate trial purchase
       if product_type == 'trial' and user.has_used_trial:
           return jsonify({'error': 'Trial plan can only be purchased once'}), 400

       # Validate top-up purchase
       if product_type == 'top_up' and not user.stripe_subscription_id:
           return jsonify({'error': 'Must have an active subscription to purchase top-ups'}), 400

       if not user.stripe_customer_id:
           customer = stripe.Customer.create(
               email=email,
               metadata={"user_id": user.id}
           )
           user.stripe_customer_id = customer.id
           db.commit()

       checkout_session = stripe.checkout.Session.create(
           customer=user.stripe_customer_id,
           payment_method_types=['card'],
           line_items=[{'price': price_id, 'quantity': 1}],
           mode='subscription' if product_type == 'subscription_plan' else 'payment',
           currency=get_currency_for_country(country_code).lower(),
           success_url=f"{request.headers.get('Origin', 'http://localhost:5173')}/success?session_id={{CHECKOUT_SESSION_ID}}",
           cancel_url=f"{request.headers.get('Origin', 'http://localhost:5173')}/cancel",
           allow_promotion_codes=True,
           expand=['line_items']
       )

       return jsonify({
           'sessionId': checkout_session.id,
           'url': checkout_session.url
       }), 200

   except Exception as e:
       db.rollback()
       return jsonify({'error': str(e)}), 500
   finally:
       db.close()

@app.route('/webhook', methods=['POST'])
def webhook():
    db = SessionLocal()
    try:
        payload = request.data
        sig_header = request.headers.get('Stripe-Signature')
        event = stripe.Webhook.construct_event(
            payload, 
            sig_header, 
            os.getenv('STRIPE_WEBHOOK_SECRET', 'whsec_53bfa6201823d0ee2843078fe37cfc21ef5e0ff602d804d282b8b550cc77dab5')
        )
        print(f"Received webhook event: {event['type']}")
        
        # for catching the buying of top-up and trial
        if event['type'] == 'checkout.session.completed':
            session = event['data']['object']
            
            if session.mode == 'payment':
                session_with_items = stripe.checkout.Session.retrieve(
                    session.id,
                    expand=['line_items']
                )

                price = session_with_items.line_items.data[0].price
                product_id = price.product
                product = stripe.Product.retrieve(product_id)
                customer = stripe.Customer.retrieve(session.customer)
                user = get_user_by_email(customer.email, db)
                
                if product.metadata.get('type') == 'trial':
                    trial_days = int(product.metadata.get('expiration_in_days', 0))
                    trial_end = datetime.utcnow() + timedelta(days=trial_days)
                    
                    user.has_used_trial = True
                    user.trial_end_date = trial_end
                    
                    credits = int(product.metadata.get('base_credits', 0))
                    scans = int(product.metadata.get('base_scans', 0))
                    
                    # Transaction for credits
                    credit_transaction = Transaction(
                        user_id=user.id,
                        primary_type='credit',
                        source_type='trial',
                        transaction_type='received',
                        value=credits,
                        payment_id=session.id,
                        description="Trial plan credits"
                    )
                    db.add(credit_transaction)
                    
                    # Transaction for scans
                    scan_transaction = Transaction(
                        user_id=user.id,
                        primary_type='scan',
                        source_type='trial',
                        transaction_type='received',
                        value=scans,
                        payment_id=session.id,
                        description="Trial plan scans"
                    )
                    db.add(scan_transaction)
                    
                    user.current_credits = credits
                    user.current_scans = scans
                    user.max_users = int(product.metadata.get('users', 0))

                elif product.metadata.get('type') == 'top_up':
                    quantity = int(price.metadata.get('quantity', 0))
                    if product.metadata.get('top_up_type') == 'credit':
                        user.current_credits += quantity
                        
                        credit_transaction = Transaction(
                            user_id=user.id,
                            primary_type='credit',
                            source_type='top_up',
                            transaction_type='received',
                            value=quantity,
                            payment_id=session.id,
                            description="Credit top-up"
                        )
                        db.add(credit_transaction)
                        
                    elif product.metadata.get('top_up_type') == 'scan':
                        user.current_scans += quantity
                        
                        scan_transaction = Transaction(
                            user_id=user.id,
                            primary_type='scan',
                            source_type='top_up',
                            transaction_type='received',
                            value=quantity,
                            payment_id=session.id,
                            description="Scan top-up"
                        )
                        db.add(scan_transaction)

                db.commit()

                

        # for catching the buying of subscription as well as renewal.
        elif event['type'] == 'invoice.paid':
            invoice = event['data']['object']
            subscription_id = invoice.get('subscription')
            
            if subscription_id:
                subscription = stripe.Subscription.retrieve(subscription_id)
                product = stripe.Product.retrieve(subscription.plan.product)
                customer = stripe.Customer.retrieve(invoice.customer)
                user = get_user_by_email(customer.email, db)
                
                # Get subscription details
                is_yearly = subscription.plan.interval == 'year'
                credits_key = 'base_credits_yearly' if is_yearly else 'base_credits_monthly'
                scans_key = 'base_scans_yearly' if is_yearly else 'base_scans_monthly'
                
                credits = int(product.metadata.get(credits_key, 0))
                scans = int(product.metadata.get(scans_key, 0))

                # Check if this is a subscription renewal
                is_renewal = invoice.get('billing_reason') == 'subscription_cycle'
                
                if is_renewal and user.stripe_subscription_id == subscription_id:
                    print(f"Processing subscription renewal for user {user.email}")
                    print(f"Previous credits: {user.current_credits}, Previous scans: {user.current_scans}")
                    
                    # Create transactions for renewal credits and scans
                    credit_transaction = Transaction(
                        user_id=user.id,
                        primary_type='credit',
                        source_type='subscription',
                        transaction_type='received',
                        value=credits,
                        subscription_id=subscription_id,
                        description=f'Renewal credits for {product.name} subscription'
                    )
                    db.add(credit_transaction)
                    
                    scan_transaction = Transaction(
                        user_id=user.id,
                        primary_type='scan',
                        source_type='subscription',
                        transaction_type='received',
                        value=scans,
                        subscription_id=subscription_id,
                        description=f'Renewal scans for {product.name} subscription'
                    )
                    db.add(scan_transaction)
                    
                    # Add new values to existing ones
                    user.current_credits += credits
                    user.current_scans += scans
                    
                    print(f"New totals - Credits: {user.current_credits}, Scans: {user.current_scans}")

                elif user.is_blocked:
                    # For blocked users, add new credits/scans to existing ones
                    print(f"Resubscription for blocked user {user.email}")
                    print(f"Previous credits: {user.current_credits}, Previous scans: {user.current_scans}")
                    
                    # Create transactions for additional credits and scans
                    credit_transaction = Transaction(
                        user_id=user.id,
                        primary_type='credit',
                        source_type='subscription',
                        transaction_type='received',
                        value=credits,
                        subscription_id=subscription_id,
                        description=f'Additional credits from resubscription to {product.name}'
                    )
                    db.add(credit_transaction)
                    
                    scan_transaction = Transaction(
                        user_id=user.id,
                        primary_type='scan',
                        source_type='subscription',
                        transaction_type='received',
                        value=scans,
                        subscription_id=subscription_id,
                        description=f'Additional scans from resubscription to {product.name}'
                    )
                    db.add(scan_transaction)
                    
                    # Add new values to existing ones
                    user.current_credits += credits
                    user.current_scans += scans
                    user.max_users = int(product.metadata.get('users', 0))
                    
                    # Reset blocked status
                    user.is_blocked = False
                    user.benefits_end_date = None
                    user.stripe_subscription_id = subscription_id
                    
                    print(f"New totals - Credits: {user.current_credits}, Scans: {user.current_scans}")

                elif not user.stripe_subscription_id or invoice.get('billing_reason') == 'subscription_create':
                    # Brand new subscription
                    print(f"New subscription for user {user.email}")
                    
                    # If transitioning from trial, create reset transactions
                    if user.has_used_trial:
                        credit_reset_transaction = Transaction(
                            user_id=user.id,
                            primary_type='credit',
                            source_type='subscription',
                            transaction_type='reset',
                            value=user.current_credits,
                            subscription_id=subscription_id,
                            description='Reset credits from trial to subscription'
                        )
                        db.add(credit_reset_transaction)

                        scan_reset_transaction = Transaction(
                            user_id=user.id,
                            primary_type='scan',
                            source_type='subscription',
                            transaction_type='reset',
                            value=user.current_scans,
                            subscription_id=subscription_id,
                            description='Reset scans from trial to subscription'
                        )
                        db.add(scan_reset_transaction)
                    
                    # Create transactions for initial credits and scans
                    credit_transaction = Transaction(
                        user_id=user.id,
                        primary_type='credit',
                        source_type='subscription',
                        transaction_type='received',
                        value=credits,
                        subscription_id=subscription_id,
                        description=f'Initial credits for {product.name} subscription'
                    )
                    db.add(credit_transaction)
                    
                    scan_transaction = Transaction(
                        user_id=user.id,
                        primary_type='scan',
                        source_type='subscription',
                        transaction_type='received',
                        value=scans,
                        subscription_id=subscription_id,
                        description=f'Initial scans for {product.name} subscription'
                    )
                    db.add(scan_transaction)
                    
                    # Set initial values
                    user.current_credits = credits
                    user.current_scans = scans
                    user.max_users = int(product.metadata.get('users', 0))
                    user.stripe_subscription_id = subscription_id
                    user.has_used_trial = True
                    user.trial_end_date = None
                
                db.commit()
                print(f"Successfully processed subscription update for user {user.email}")

        # catch the subscription updated .
        elif event['type'] == 'customer.subscription.updated':
            subscription = event['data']['object']
            previous_attributes = event['data']['previous_attributes']
            
            # Only process price updates
            if 'items' in previous_attributes and 'data' in previous_attributes['items']:
                # Get customer and user
                customer_id = subscription['customer']
                customer = stripe.Customer.retrieve(customer_id)
                user = get_user_by_email(customer.email, db)
                
                if not user:
                    raise Exception(f"User not found for customer {customer_id}")
                
                # Get the new product details
                new_price_id = subscription['items']['data'][0]['price']['id']
                new_price = stripe.Price.retrieve(new_price_id)
                new_product = stripe.Product.retrieve(new_price['product'])
                
                # Get the old product details
                old_price_id = previous_attributes['items']['data'][0]['price']['id']
                old_price = stripe.Price.retrieve(old_price_id)
                old_product = stripe.Product.retrieve(old_price['product'])
                
                # Determine if it's monthly or yearly subscription
                is_yearly = subscription['items']['data'][0]['plan']['interval'] == 'year'
                credits_key = 'base_credits_yearly' if is_yearly else 'base_credits_monthly'
                scans_key = 'base_scans_yearly' if is_yearly else 'base_scans_monthly'
                
                # Get new limits from product metadata
                new_credits = int(new_product.metadata.get(credits_key, 0))
                new_scans = int(new_product.metadata.get(scans_key, 0))
                new_max_users = int(new_product.metadata.get('users', 0))
                
                # Create transactions for additional credits and scans
                credit_transaction = Transaction(
                    user_id=user.id,
                    primary_type='credit',
                    source_type='subscription',
                    transaction_type='received',
                    value=new_credits,
                    subscription_id=subscription.id,
                    description=f'Additional credits from upgrade: {old_product.name} to {new_product.name}'
                )
                db.add(credit_transaction)
                
                scan_transaction = Transaction(
                    user_id=user.id,
                    primary_type='scan',
                    source_type='subscription',
                    transaction_type='received',
                    value=new_scans,
                    subscription_id=subscription.id,
                    description=f'Additional scans from upgrade: {old_product.name} to {new_product.name}'
                )
                db.add(scan_transaction)
                
                # Add new values to existing credits and scans
                user.current_credits += new_credits
                user.current_scans += new_scans
                user.max_users = new_max_users 
                
                db.commit()
                print(f"Successfully processed subscription upgrade for user {user.email}")
                print(f"Previous credits: {user.current_credits - new_credits}, Previous scans: {user.current_scans - new_scans}")
                print(f"Added credits: {new_credits}, Added scans: {new_scans}")
                print(f"New totals - Credits: {user.current_credits}, Scans: {user.current_scans}, Max Users: {new_max_users}")

        # catch subscription deleted.
        elif event['type'] == 'customer.subscription.deleted':
            subscription = event['data']['object']
            
            # Get customer and user
            customer_id = subscription['customer']
            customer = stripe.Customer.retrieve(customer_id)
            user = get_user_by_email(customer.email, db)
            
            if not user:
                raise Exception(f"User not found for customer {customer_id}")
            
            # Set blocked status and benefits end date (3 months from now)
            user.is_blocked = True
            user.benefits_end_date = datetime.utcnow() + timedelta(days=90)  # 3 months
            
            print(f"Subscription cancelled for user {user.email}. Benefits will expire on {user.benefits_end_date}")
            db.commit()    
        return jsonify({'status': 'success'}), 200
        
    except Exception as e:
        db.rollback()
        print(f"Error processing event: {e}")
        return jsonify({'error': (e)}), 500
    finally:
        db.close()

@app.route('/api/test/emails', methods=['POST'])
def test_emails():
    try:
        data = request.get_json()
        to_email = data.get('email')
        email_type = data.get('type', 'trial_expiry')  # default type
    
        if not to_email:
            return jsonify({'error': 'Email address is required'}), 400

        email_result = False
        
        # Test different email types
        if email_type == 'trial_expiry':
            email_result = email_service.send_trial_expiration_notice(
                to_email, 
                days_remaining=3
            )
        
        elif email_type == 'payment_blocked':
            due_date = datetime.utcnow() + timedelta(days=7)
            email_result = email_service.send_payment_blocked_notice(
                to_email, 
                due_date=due_date
            )
        
        elif email_type == 'subscription_cancelled':
            benefits_end = datetime.utcnow() + timedelta(days=90)
            email_result = email_service.send_subscription_cancelled_notice(
                to_email, 
                benefits_end_date=benefits_end
            )
        
        elif email_type == 'benefits_expiring':
            email_result = email_service.send_benefits_expiring_notice(
                to_email, 
                days_remaining=7
            )
        
        elif email_type == 'payment_success':
            email_result = email_service.send_payment_successful_notice(
                to_email, 
                plan_name="Pro Plan",
                amount="$99.99"
            )
        else:
            return jsonify({'error': 'Invalid email type'}), 400

        if email_result:
            return jsonify({
                'status': 'success',
                'message': f'Successfully sent {email_type} email to {to_email}'
            }), 200
        else:
            return jsonify({
                'status': 'error',
                'message': f'Failed to send {email_type} email'
            }), 500

    except Exception as e:
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500

@app.route('/api/transactions', methods=['GET'])
def get_transactions():
    db = SessionLocal()
    try:
        # Get user_id from query params
        user_id = request.args.get('user_id')
        
        if not user_id:
            return jsonify({'error': 'user_id is required'}), 400
            
        # Get optional filters
        transaction_type = request.args.get('type')  # received, used, reset
        source_type = request.args.get('source')     # subscription, top_up, trial
        primary_type = request.args.get('primary')   # credit, scan
        
        # Start with base query
        query = db.query(Transaction).filter(Transaction.user_id == user_id)
        
        # Apply optional filters
        if transaction_type:
            query = query.filter(Transaction.transaction_type == transaction_type)
        if source_type:
            query = query.filter(Transaction.source_type == source_type)
        if primary_type:
            query = query.filter(Transaction.primary_type == primary_type)
            
        # Order by created_at descending (newest first)
        transactions = query.order_by(Transaction.created_at.desc()).all()
        
        # Format response
        transaction_list = []
        for transaction in transactions:
            transaction_list.append({
                'id': transaction.id,
                'primary_type': transaction.primary_type,
                'source_type': transaction.source_type,
                'transaction_type': transaction.transaction_type,
                'value': transaction.value,
                'subscription_id': transaction.subscription_id,
                'payment_id': transaction.payment_id,
                'description': transaction.description,
                'created_at': transaction.created_at.isoformat()
            })
            
        # Get summary statistics
        summary = {
            'total_credits_received': sum(t.value for t in transactions 
                if t.primary_type == 'credit' and t.transaction_type == 'received'),
            'total_credits_used': sum(t.value for t in transactions 
                if t.primary_type == 'credit' and t.transaction_type == 'used'),
            'total_credits_reset': sum(t.value for t in transactions 
                if t.primary_type == 'credit' and t.transaction_type == 'reset'),
            'total_scans_received': sum(t.value for t in transactions 
                if t.primary_type == 'scan' and t.transaction_type == 'received'),
            'total_scans_used': sum(t.value for t in transactions 
                if t.primary_type == 'scan' and t.transaction_type == 'used'),
            'total_scans_reset': sum(t.value for t in transactions 
                if t.primary_type == 'scan' and t.transaction_type == 'reset')
        }
        
        return jsonify({
            'transactions': transaction_list,
            'summary': summary,
            'filters': {
                'transaction_type': transaction_type,
                'source_type': source_type,
                'primary_type': primary_type
            },
            'total_count': len(transaction_list)
        }), 200

    except Exception as e:
        print(f"Error fetching transactions: {str(e)}")
        return jsonify({'error': str(e)}), 500
    finally:
        db.close()

def extract_user_data(event):
    invoice = event['data']['object']
    
    return {
        'customer_id': invoice['customer'],
        'customer_email': invoice['customer_email'],
        'customer_name': invoice['customer_name'],
        'subscription_id': invoice['subscription'],
        'amount_paid': invoice['amount_paid'],  # 1600 (in pennies)
        'currency': invoice['currency'],        # 'gbp'
        'invoice_id': invoice['id'],
        'status': invoice['status'],
        'payment_intent': invoice['payment_intent'],
        'address': {
            'city': invoice['customer_address']['city'],
            'country': invoice['customer_address']['country'],
            'line1': invoice['customer_address']['line1'],
            'postal_code': invoice['customer_address']['postal_code'],
            'state': invoice['customer_address']['state']
        }
    }

def check_trial_expiration():
    print(f"Running trial expiration check at {datetime.utcnow()}")
    db = SessionLocal()
    try:
        # Get all trial users whose trial has expired
        expired_trials = db.query(User).filter(
            User.trial_end_date.isnot(None),
            User.trial_end_date < datetime.utcnow()
        ).all()
        
        for user in expired_trials:
            print(f"Processing expired trial for user {user.email}")
            
            # Create reset transactions
            if user.current_credits > 0:
                credit_reset_transaction = Transaction(
                    user_id=user.id,
                    primary_type='credit',
                    source_type='trial',
                    transaction_type='reset',
                    value=user.current_credits,
                    description='Reset credits due to trial expiration'
                )
                db.add(credit_reset_transaction)
            
            if user.current_scans > 0:
                scan_reset_transaction = Transaction(
                    user_id=user.id,
                    primary_type='scan',
                    source_type='trial',
                    transaction_type='reset',
                    value=user.current_scans,
                    description='Reset scans due to trial expiration'
                )
                db.add(scan_reset_transaction)
            
            # Reset user limits
            user.current_credits = 0
            user.current_scans = 0
            user.max_users = 0
            user.trial_end_date = None  # Clear trial end date
            
            print(f"Trial expired and limits reset for user {user.email}")
        
        db.commit()
        print(f"Processed {len(expired_trials)} expired trials")
        
    except Exception as e:
        db.rollback()
        print(f"Error in trial expiration check: {str(e)}")
    finally:
        db.close()

def check_benefits_expiration():
    
    print(f"Running benefits expiration check at {datetime.utcnow()}")
    db = SessionLocal()
    try:
        # Get all blocked users whose benefits have expired
        expired_benefits = db.query(User).filter(
            User.is_blocked == True,
            User.benefits_end_date.isnot(None),
            User.benefits_end_date < datetime.utcnow()
        ).all()
        
        for user in expired_benefits:
            print(f"Processing expired benefits for user {user.email}")
            
            # Create reset transactions
            credit_reset_transaction = Transaction(
                user_id=user.id,
                primary_type='credit',
                source_type='cancel_subscription',
                transaction_type='reset',
                value=user.current_credits,
                subscription_id=user.stripe_subscription_id,
                description='Reset credits due to benefits expiration'
            )
            db.add(credit_reset_transaction)
            
            scan_reset_transaction = Transaction(
                user_id=user.id,
                primary_type='scan',
                source_type='cancel_subscription',
                transaction_type='reset',
                value=user.current_scans,
                subscription_id=user.stripe_subscription_id,
                description='Reset scans due to benefits expiration'
            )
            db.add(scan_reset_transaction)
            
            # Reset user limits
            user.current_credits = 0
            user.current_scans = 0
            user.max_users = 0
            user.is_blocked = False
            user.stripe_subscription_id = None  # Now we can remove the subscription ID
            user.benefits_end_date = None  # Clear the benefits end date
            
            print(f"Benefits expired and limits reset for user {user.email}")
        
        db.commit()
        print(f"Processed {len(expired_benefits)} expired benefits")
        
    except Exception as e:
        db.rollback()
        print(f"Error in benefits expiration check: {str(e)}")
    finally:
        db.close()

# Initialize the scheduler
scheduler = BackgroundScheduler()
scheduler.add_job(
    check_trial_expiration,
    CronTrigger(hour=0, minute=0),  # Run at midnight every day
    id='trial_expiration_check',
    name='Check for expired trials',
    replace_existing=True
)

scheduler.add_job(
    check_benefits_expiration,
    CronTrigger(hour=0, minute=0),  # Run at midnight every day
    id='benefits_expiration_check',
    name='Check for expired benefits',
    replace_existing=True
)

if __name__ == '__main__':
    try:
        # Start the background scheduler
        scheduler.start()
        
        # Start the Flask app
        app.run(host='0.0.0.0', port=5000)
    except (KeyboardInterrupt, SystemExit):
        
        scheduler.shutdown()


# @app.route('/api/create-subscription-invoice', methods=['POST'])
# def create_subscription_invoice():
#     try:
#         data = request.get_json()
#         email = data.get('email')
#         price_id = data.get('priceId')
#         country_code = data.get('countryCode', 'IN')

#         if not email or not price_id:
#             return jsonify({"error": "Email and priceId are required"}), 400

#         price = stripe.Price.retrieve(price_id)
#         if not price.recurring:
#             return jsonify({"error": "Provided price ID is not for a subscription"}), 400

#         # Get or create customer
#         customers = stripe.Customer.list(email=email, limit=1)
#         if customers.data:
#             customer = customers.data[0]
#             customer = stripe.Customer.modify(
#                 customer.id,
#                 address={'country': country_code}
#             )
#         else:
#             customer = stripe.Customer.create(
#                 email=email,
#                 address={'country': country_code},
#                 preferred_locales=[country_code.lower()],
#                 metadata={
#                     "source": "api",
#                     "country": country_code
#                 }
#             )

#         # Determine collection method based on country and amount
#         collection_method = 'send_invoice' if price.unit_amount >= 50000 else 'charge_automatically'

#         subscription = stripe.Subscription.create(
#             customer=customer.id,
#             items=[{"price": price_id}],
#             collection_method=collection_method,
#             days_until_due=30 if collection_method == 'send_invoice' else None,
#             payment_settings={
#                 'payment_method_types': get_payment_methods_for_country(country_code)
#             },
#             metadata={
#                 "source": "api",
#                 "country": country_code
#             }
#         )

#         response_data = {
#             "subscription_id": subscription.id,
#             "current_period_end": subscription.current_period_end,
#             "billing_cycle": {
#                 "interval": price.recurring.interval,
#                 "interval_count": price.recurring.interval_count
#             }
#         }

#         if collection_method == 'send_invoice':
#             invoice = stripe.Invoice.retrieve(subscription.latest_invoice)
#             if invoice.status == 'draft':
#                 invoice = stripe.Invoice.finalize_invoice(invoice.id)
#                 invoice = stripe.Invoice.send_invoice(invoice.id)

#             response_data.update({
#                 "invoice_id": invoice.id,
#                 "invoice_url": invoice.hosted_invoice_url,
#                 "pdf_url": invoice.invoice_pdf,
#                 "amount_due": invoice.amount_due / 100,
#                 "due_date": invoice.due_date
#             })

#         return jsonify(response_data), 200

#     except Exception as e:
#         print(f"Error in create_subscription_invoice: {str(e)}")
#         return jsonify({"error": str(e)}), 500
