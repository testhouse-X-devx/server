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
    role = Column(String(50), default='admin')

    # Validity and expiration management
    validity_expiration = Column(DateTime, nullable=True)
    credit_cleanup_date = Column(DateTime, nullable=True)  # 30 days after validity expiration
    account_deletion_date = Column(DateTime, nullable=True)  # 150 days after credit cleanup

    # Admin-specific columns
    stripe_customer_id = Column(String(255), unique=True, nullable=True)
    stripe_subscription_id = Column(String(255), unique=True, nullable=True)
    has_used_trial = Column(Boolean, default=False)
    trial_end_date = Column(DateTime, nullable=True)
    
    is_blocked = Column(Boolean, default=False)
    benefits_end_date = Column(DateTime, nullable=True)
    
    # Credits
    current_user_story = Column(Integer, default=0)
    current_test_case = Column(Integer, default=0)
    
    # Team member creation tracking
    created_by = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Soft delete
    is_deleted = Column(Boolean, default=False)

class Transaction(Base):
    __tablename__ = 'transactions'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'))
    primary_type = Column(String(50), nullable=False)  # 'credit' or 'scan' or 'generation'
    source_type = Column(String(50), nullable=False)   # 'subscription', 'bundle', or 'trial' 
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
        include_trials = request.args.get('include_trials', 'true').lower() == 'true'
        
        # Create search query for metadata
        search_query = 'metadata["type"]:"bundle" OR metadata["type"]:"trial"'
        
        # Search products with metadata filter
        products = stripe.Product.search(
            query=search_query,
            limit=100
        )
        
        print(f"Number of products found: {len(products.data)}")
        product_data = []

        for product in products.data:
            # Skip inactive products
            if not product.active:
                continue

            # Check if product is a trial product
            is_trial = product.metadata.get('type', '').lower() == 'trial'
            
            # Skip trial products if not requested
            if not include_trials and is_trial:
                continue

            # Get base price first
            prices = stripe.Price.list(product=product.id)
            if not prices.data:
                continue
                
            base_price = prices.data[0]  # Using first price as base price
            
            # Rest of your existing code stays exactly the same...
            if is_trial:
                trial_entry = {
                    "id": product.id,
                    "name": product.name,
                    "description": product.description,
                    "type": "trial",
                    "validity_in_days": int(product.metadata.get('validity_in_days', 14)),
                    "credits": {
                        "test_case": int(product.metadata.get('test_case', 0)),
                        "user_story": int(product.metadata.get('user_story', 0))
                    },
                    "price": {
                        "amount": 0,
                        "price_id": base_price.id
                    }
                }
                product_data.append(trial_entry)
                continue

            # Get base units from transform_quantity.divide_by
            base_units = base_price.transform_quantity.get('divide_by') if base_price.transform_quantity else 1
            base_unit_amount = base_price.unit_amount / base_units

            # Your existing code for credit options and pricing continues...
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
                "type": "regular",
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
                "selected_option": selected_option or "all",
                "include_trials": include_trials
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

def validate_product_combination(items):
    """
    Validate that trial and regular products are not mixed.
    Returns a tuple of (is_valid, error_message, has_trial, has_regular, bundle_quantities)
    """
    try:
        has_trial = False
        has_regular = False
        bundle_quantities = {
            'test_case': 0,
            'user_story': 0
        }
        
        # Fetch all prices and their associated products
        for item in items:
            price_id = item.get('priceId')
            if not price_id:
                continue
                
            # Get price and its associated product
            price = stripe.Price.retrieve(price_id, expand=['product'])
            if not price.product:
                continue
                
            # Check product type from metadata
            is_trial = price.product.metadata.get('type', '').lower() == 'trial'
            
            if is_trial:
                has_trial = True
            else:
                has_regular = True
                # Collect quantities for each bundle type
                bundle_type = price.product.metadata.get('bundle_type', '').lower()
                if bundle_type in bundle_quantities:
                    bundle_quantities[bundle_type] += item.get('credits', 0)
            
            if has_trial and has_regular:
                return (
                    False, 
                    'Cannot combine trial plans with regular plans. Please select either a trial plan or regular plans.',
                    has_trial,
                    has_regular,
                    bundle_quantities
                )

        return (True, None, has_trial, has_regular, bundle_quantities)

    except stripe.error.StripeError as e:
        return (False, f'Error validating plans: {str(e)}', False, False, bundle_quantities)

def find_matching_subscription_product(bundle_quantities):
    """Find existing subscription product with matching metadata."""
    products = stripe.Product.list(
        active=True,
        limit=100
    )
    
    for product in products.data:
        if (product.metadata.get('type') == 'subscription' and
            product.metadata.get('test_case') == str(bundle_quantities['test_case']) and
            product.metadata.get('user_story') == str(bundle_quantities['user_story']) and
            product.metadata.get('interval') == '3_month'):
            return product
            
    return None

def generate_subscription_product_name(bundle_quantities):
    """Generate a descriptive name for the subscription product."""
    parts = []
    if bundle_quantities['test_case']:
        parts.append(f"{bundle_quantities['test_case']} Test Cases")
    if bundle_quantities['user_story']:
        parts.append(f"{bundle_quantities['user_story']} User Stories")
    
    return f"3-Month Subscription: {' + '.join(parts)}"

def calculate_subscription_price(items):
    """Calculate subscription price based on original bundle prices and quantities."""
    total_amount = 0
    
    for item in items:
        price_id = item.get('priceId')
        quantity = item.get('credits', 0)
        
        if price_id and quantity:
            # Get original price info
            price = stripe.Price.retrieve(price_id)
            
            # Get base units from transform_quantity
            base_units = price.transform_quantity.get('divide_by') if price.transform_quantity else 1
            base_unit_amount = price.unit_amount / base_units
            
            # Calculate amount for this bundle
            bundle_amount = base_unit_amount * quantity
            total_amount += bundle_amount
    
    return int(total_amount)  # Ensure we return an integer amount in cents

@app.route('/api/create-checkout-session', methods=['POST'])
def create_checkout_session():
    db = SessionLocal()
    try:
        data = request.get_json()
        email = data.get('email')
        items = data.get('items', [])  # Array of {priceId, credits}
        is_subscription = data.get('isSubscription', False)
        country_code = 'GB'

        if not email:
            return jsonify({'error': 'Email is required'}), 400
            
        if not items:
            return jsonify({'error': 'No items selected'}), 400

        # Validate plan combination
        is_valid, error_message, has_trial, has_regular, bundle_quantities = validate_product_combination(items)
        
        if not is_valid:
            return jsonify({'error': error_message}), 400

        if has_trial and is_subscription:
            return jsonify({
                'error': 'Trial plans cannot be converted to subscriptions.'
            }), 400

        # Get or create user
        user = get_user_by_email(email, db)
        if not user:
            user = User(email=email)
            db.add(user)
        elif user.stripe_subscription_id and not is_subscription:
            # If user has active subscription and trying to buy bundle
            return jsonify({
                'error': 'You already have an active subscription. Please manage your subscription instead of buying bundles.'
            }), 400
        
        # Get or create Stripe customer
        if not user.stripe_customer_id:
            customer = stripe.Customer.create(
                email=email,
                metadata={"user_id": user.id}
            )
            user.stripe_customer_id = customer.id
            db.commit()

        if is_subscription:
            # First, check if a matching subscription product exists
            existing_product = find_matching_subscription_product(bundle_quantities)
            
            if not existing_product:
                # Create new subscription product with metadata
                product_name = generate_subscription_product_name(bundle_quantities)
                product = stripe.Product.create(
                    name=product_name,
                    metadata={
                        'type': 'subscription',
                        'test_case': str(bundle_quantities['test_case']),
                        'user_story': str(bundle_quantities['user_story']),
                        'interval': '3_month'
                    }
                )
                
                # Create recurring price for the product
                price = stripe.Price.create(
                    product=product.id,
                    unit_amount=calculate_subscription_price(items),
                    currency=get_currency_for_country(country_code).lower(),
                    recurring={
                        'interval': 'month',
                        'interval_count': 3
                    }
                )
            else:
                # Get existing price for the product
                prices = stripe.Price.list(
                    product=existing_product.id,
                    active=True,
                    limit=1
                )
                price = prices.data[0]

            # Create subscription checkout session
            checkout_session = stripe.checkout.Session.create(
                customer=user.stripe_customer_id,
                payment_method_types=['card'],
                line_items=[{
                    'price': price.id,
                    'quantity': 1
                }],
                mode='subscription',
                currency=get_currency_for_country(country_code).lower(),
                success_url=f"{request.headers.get('Origin', 'http://localhost:5173')}/success?session_id={{CHECKOUT_SESSION_ID}}",
                cancel_url=f"{request.headers.get('Origin', 'http://localhost:5173')}/cancel",
                allow_promotion_codes=True,
                
            )
        else:
            # Regular one-time payment checkout
            line_items = []
            metadata = {}
            
            for item in items:
                price_id = item.get('priceId')
                credits = item.get('credits')
                
                if price_id and credits:
                    if isinstance(credits, dict):  # Trial plan
                        metadata['test_case_credits'] = credits.get('test_case', 0)
                        metadata['user_story_credits'] = credits.get('user_story', 0)
                        line_items.append({
                            'price': price_id,
                            'quantity': 1
                        })
                    else:
                        line_items.append({
                            'price': price_id,
                            'quantity': credits
                        })

            checkout_session = stripe.checkout.Session.create(
                customer=user.stripe_customer_id,
                payment_method_types=['card'],
                line_items=line_items,
                mode='payment',
                currency=get_currency_for_country(country_code).lower(),
                success_url=f"{request.headers.get('Origin', 'http://localhost:5173')}/success?session_id={{CHECKOUT_SESSION_ID}}",
                cancel_url=f"{request.headers.get('Origin', 'http://localhost:5173')}/cancel",
                allow_promotion_codes=True,
                metadata=metadata,
                
            )

        return jsonify({
            'sessionId': checkout_session.id,
            'url': checkout_session.url
        }), 200

    except Exception as e:
        db.rollback()
        print(f"Error in create_checkout_session: {str(e)}")
        return jsonify({'error': str(e)}), 500
    finally:
        db.close()
        
@app.route('/api/subscription', methods=['GET'])
def get_customer_subscription():
    db = SessionLocal()
    try:
        email = request.args.get('email')
        if not email:
            return jsonify({'error': 'Email is required'}), 400

        user = db.query(User).filter(User.email == email).first()
        if not user:
            return jsonify({'error': 'User not found'}), 404

        if not user.stripe_subscription_id:
            return jsonify({'subscription': None}), 200

        try:
            subscription = stripe.Subscription.retrieve(
                user.stripe_subscription_id
            )

            subscription_data = {
                'id': subscription.id,
                'status': subscription.status,
                'current_period_end': subscription.current_period_end,
                'current_period_start': subscription.current_period_start,
                'cancel_at_period_end': subscription.cancel_at_period_end,
                'default_payment_method': subscription.default_payment_method,
                'price': {
                    'amount': subscription.plan.amount / 100,
                    'currency': subscription.plan.currency.upper(),
                    'interval': subscription.plan.interval,
                    'interval_count': subscription.plan.interval_count
                }
            }

            return jsonify({
                'subscription': subscription_data
            }), 200

        except stripe.error.InvalidRequestError as e:
            if 'No such subscription' in str(e):
                user.stripe_subscription_id = None
                db.commit()
                return jsonify({'subscription': None}), 200
            raise e

    except stripe.error.InvalidRequestError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        print(f"Error fetching subscription: {str(e)}")
        return jsonify({'error': str(e)}), 500
    finally:
        db.close()
@app.route('/api/create-portal-session', methods=['POST'])
def create_portal_session():
    db = SessionLocal()
    try:
        data = request.get_json()
        email = data.get('email')
        
        if not email:
            return jsonify({'error': 'Email is required'}), 400

        # Get user from database
        user = db.query(User).filter(User.email == email).first()
        if not user:
            return jsonify({'error': 'User not found'}), 404

        if not user.stripe_subscription_id:
            return jsonify({'error': 'No subscription found for this user'}), 404

        # Create billing portal session
        session = stripe.billing_portal.Session.create(
            customer=user.stripe_customer_id,
            return_url=f"{request.headers.get('Origin', 'http://localhost:5173')}/subscriptions"
        )

        return jsonify({
            'url': session.url
        }), 200

    except Exception as e:
        print(f"Error creating portal session: {str(e)}")
        return jsonify({'error': str(e)}), 500
    finally:
        db.close()


def reset_trial_credits(user, session_id, db):
    """Reset trial credits and create reset transactions."""
    # First check if we have already reset these trial credits
    reset_transactions = db.query(Transaction).filter(
        Transaction.user_id == user.id,
        Transaction.source_type == 'trial',
        Transaction.transaction_type == 'reset'
    ).all()

    # If we have reset transactions, don't reset again
    if reset_transactions:
        return []

    # Get trial credit transactions
    trial_transactions = db.query(Transaction).filter(
        Transaction.user_id == user.id,
        Transaction.source_type == 'trial',
        Transaction.transaction_type == 'received'
    ).all()

    transactions = []
    if trial_transactions:
        trial_test_cases = sum(t.value for t in trial_transactions if t.primary_type == 'test_case')
        trial_user_stories = sum(t.value for t in trial_transactions if t.primary_type == 'user_story')

        if trial_test_cases > 0:
            transactions.append(
                Transaction(
                    user_id=user.id,
                    primary_type='test_case',
                    source_type='trial',
                    transaction_type='reset',
                    value=-trial_test_cases,
                    payment_id=session_id,
                    description="Reset trial test case credits"
                )
            )
            user.current_test_case -= trial_test_cases

        if trial_user_stories > 0:
            transactions.append(
                Transaction(
                    user_id=user.id,
                    primary_type='user_story',
                    source_type='trial',
                    transaction_type='reset',
                    value=-trial_user_stories,
                    payment_id=session_id,
                    description="Reset trial user story credits"
                )
            )
            user.current_user_story -= trial_user_stories

    return transactions
def process_trial_product(user, product, session_id):
    """Process trial product purchase and return transactions."""
    trial_days = int(product.metadata.get('expiration_in_days', 0))
    trial_end = datetime.utcnow() + timedelta(days=trial_days)
    
    user.has_used_trial = True
    user.trial_end_date = trial_end

    test_case = int(product.metadata.get('test_case', 0))
    user_story = int(product.metadata.get('user_story', 0))
    
    transactions = [
        Transaction(
            user_id=user.id,
            primary_type='test_case',
            source_type='trial',
            transaction_type='received',
            value=test_case,
            payment_id=session_id,
            description="Trial plan test case credits"
        ),
        Transaction(
            user_id=user.id,
            primary_type='user_story',
            source_type='trial',
            transaction_type='received',
            value=user_story,
            payment_id=session_id,
            description="Trial plan user story credits"
        )
    ]
    
    user.current_test_case += test_case
    user.current_user_story += user_story
    
    return transactions

def process_bundle_product(user, product, quantity, session_id):
    """Process bundle product purchase and return transaction."""
    bundle_type = product.metadata.get('bundle_type', '').lower()
    
    user.has_used_trial = True
    user.trial_end_date = None

    if bundle_type == 'test_case':
        transaction = Transaction(
            user_id=user.id,
            primary_type='test_case',
            source_type='bundle',
            transaction_type='received',
            value=quantity,
            payment_id=session_id,
            description="Test case bundle credits"
        )
        user.current_test_case += quantity
    elif bundle_type == 'user_story':
        transaction = Transaction(
            user_id=user.id,
            primary_type='user_story',
            source_type='bundle',
            transaction_type='received',
            value=quantity,
            payment_id=session_id,
            description="User story bundle credits"
        )
        user.current_user_story += quantity
    else:
        raise ValueError(f"Unknown bundle type: {bundle_type}")
        
    return [transaction]

def handle_subscription_renewal(user, subscription_id, test_case_credits, user_story_credits):
    """Handle subscription renewal and return transactions."""
    transactions = [
        Transaction(
            user_id=user.id,
            primary_type='test_case',
            source_type='subscription',
            transaction_type='received',
            value=test_case_credits,
            subscription_id=subscription_id,
            description='Renewal test case credits for subscription'
        ),
        Transaction(
            user_id=user.id,
            primary_type='user_story',
            source_type='subscription',
            transaction_type='received',
            value=user_story_credits,
            subscription_id=subscription_id,
            description='Renewal user story credits for subscription'
        )
    ]
    
    user.current_test_case += test_case_credits
    user.current_user_story += user_story_credits
    
    return transactions

def handle_new_subscription(user, subscription_id, test_case_credits, user_story_credits):
    """Handle new subscription creation and return transactions."""
    transactions = []
    
    # Reset trial credits only if they're from a trial (both conditions must be true)
    if user.has_used_trial and user.trial_end_date:
        if user.current_test_case > 0:
            transactions.append(Transaction(
                user_id=user.id,
                primary_type='test_case',
                source_type='trial',
                transaction_type='reset',
                value=-user.current_test_case,
                subscription_id=subscription_id,
                description='Reset test case credits from trial to subscription'
            ))
            user.current_test_case = 0

        if user.current_user_story > 0:
            transactions.append(Transaction(
                user_id=user.id,
                primary_type='user_story',
                source_type='trial',
                transaction_type='reset',
                value=-user.current_user_story,
                subscription_id=subscription_id,
                description='Reset user story credits from trial to subscription'
            ))
            user.current_user_story = 0
    
    # Add subscription transactions
    transactions.extend([
        Transaction(
            user_id=user.id,
            primary_type='test_case',
            source_type='subscription',
            transaction_type='received',
            value=test_case_credits,
            subscription_id=subscription_id,
            description='Initial test case credits for subscription'
        ),
        Transaction(
            user_id=user.id,
            primary_type='user_story',
            source_type='subscription',
            transaction_type='received',
            value=user_story_credits,
            subscription_id=subscription_id,
            description='Initial user story credits for subscription'
        )
    ])
    
    # Update user properties - add to existing credits
    user.current_test_case += test_case_credits
    user.current_user_story += user_story_credits
    
    # Update subscription related fields
    user.stripe_subscription_id = subscription_id
    user.has_used_trial = True
    user.trial_end_date = None
    user.validity_expiration = datetime.utcnow() + timedelta(days=90)
    
    return transactions
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
        
        if event['type'] == 'checkout.session.completed':
            print(f"Checkout session completed: {event['data']['object']}")
            session = event['data']['object']
            
            if session.mode == 'payment':
                session_with_items = stripe.checkout.Session.retrieve(
                    session.id,
                    expand=['line_items']
                )

                customer = stripe.Customer.retrieve(session.customer)
                user = get_user_by_email(customer.email, db)

                # Handle unblocking if user was blocked but not deleted
                if user.is_blocked and not user.is_deleted:
                    user.is_blocked = False
                    user.credit_cleanup_date = None
                    user.account_deletion_date = None

                transactions = []

                # Validate product mix and get max validity
                max_validity_days = 0
                has_trial = False
                has_regular = False

                for line_item in session_with_items.line_items.data:
                    product = stripe.Product.retrieve(line_item.price.product)
                    if product.metadata.get('type', '').lower() == 'trial':
                        has_trial = True
                    else:
                        has_regular = True
                        validity_days = int(product.metadata.get('validity_in_days', 90))
                        max_validity_days = max(max_validity_days, validity_days)
                    
                    if has_trial and has_regular:
                        raise ValueError("Cannot mix trial and regular products in the same checkout")

                # Handle regular product validity and trial reset
                if has_regular:
                    user.validity_expiration = datetime.utcnow() + timedelta(days=max_validity_days)
                    transactions.extend(reset_trial_credits(user, session.id, db))

                # Process each line item
                for line_item in session_with_items.line_items.data:
                    product = stripe.Product.retrieve(line_item.price.product)
                    product_type = product.metadata.get('type', '').lower()
                    
                    if product_type == 'trial':
                        transactions.extend(process_trial_product(user, product, session.id))
                    else:
                        quantity = int(line_item.quantity)
                        transactions.extend(process_bundle_product(user, product, quantity, session.id))

                # Add all transactions
                for transaction in transactions:
                    db.add(transaction)
                
                db.commit()

        elif event['type'] == 'invoice.paid':
            invoice = event['data']['object']
            subscription_id = invoice.get('subscription')
            
            if subscription_id:
                subscription = stripe.Subscription.retrieve(subscription_id)
                product = stripe.Product.retrieve(subscription.plan.product)
                customer = stripe.Customer.retrieve(invoice.customer)
                user = get_user_by_email(customer.email, db)

                # Handle unblocking if user was blocked but not deleted
                if user.is_blocked and not user.is_deleted:
                    user.is_blocked = False
                    user.credit_cleanup_date = None
                    user.account_deletion_date = None

                test_case_credits = int(product.metadata.get('test_case', 0))
                user_story_credits = int(product.metadata.get('user_story', 0))
                is_renewal = invoice.get('billing_reason') == 'subscription_cycle'
                print(f"in renewal {user.email} (renewal: {is_renewal})")

                transactions = []
                if is_renewal and user.stripe_subscription_id == subscription_id:
                    transactions.extend(handle_subscription_renewal(
                        user, subscription_id, test_case_credits, user_story_credits
                    ))
                elif not user.stripe_subscription_id or invoice.get('billing_reason') == 'subscription_create':
                    transactions.extend(handle_new_subscription(
                        user, subscription_id, test_case_credits, user_story_credits
                    ))
                
                for transaction in transactions:
                    db.add(transaction)
                    
                db.commit()
                print(f"Successfully processed subscription update for user {user.email}")
        elif event['type'] == 'customer.subscription.deleted':
            subscription = event['data']['object']
            
            # Get customer and user
            customer_id = subscription['customer']
            print(f"Customer ID: in subscription deleted {customer_id}")
            customer = stripe.Customer.retrieve(customer_id)
            user = get_user_by_email(customer.email, db)
            
            if not user:
                raise Exception(f"User not found for customer {customer_id}")
            
            # Set blocked status and benefits end date (3 months from now)
            user.stripe_subscription_id = None
            
            
            print(f"Subscription cancelled for user {user.email}. Benefits will expire on {user.benefits_end_date}")
            db.commit()    
        return jsonify({'status': 'success'}), 200
        
    except Exception as e:
        db.rollback()
        print(f"Error processing webhook: {str(e)}")
        return jsonify({'error': str(e)}), 500
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
        user_id = request.args.get('user_id')
        
        if not user_id:
            return jsonify({'error': 'user_id is required'}), 400
            
        # Get user data first
        user = db.query(User).get(user_id)
        if not user:
            return jsonify({'error': 'User not found'}), 404
            
        # Format user data
        user_data = {
            'email': user.email,
            'current_test_case': user.current_test_case,
            'current_user_story': user.current_user_story,
            'validity_expiration': user.validity_expiration.isoformat() if user.validity_expiration else None,
            'has_used_trial': user.has_used_trial,
            'trial_end_date': user.trial_end_date.isoformat() if user.trial_end_date else None,
            'is_subscription_active': bool(user.stripe_subscription_id),
            'subscription_id': user.stripe_subscription_id
        }
            
        # Get transaction filters
        transaction_type = request.args.get('type')
        source_type = request.args.get('source')
        primary_type = request.args.get('primary')
        
        # Query transactions
        query = db.query(Transaction).filter(Transaction.user_id == user_id)
        
        if transaction_type:
            query = query.filter(Transaction.transaction_type == transaction_type)
        if source_type:
            query = query.filter(Transaction.source_type == source_type)
        if primary_type:
            query = query.filter(Transaction.primary_type == primary_type)
            
        transactions = query.order_by(Transaction.created_at.desc()).all()
        
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
            
        summary = {
            'total_credits_received': sum(t.value for t in transactions 
                if t.primary_type == 'user_story' and t.transaction_type == 'received'),
            'total_credits_used': sum(t.value for t in transactions 
                if t.primary_type == 'user_story' and t.transaction_type == 'used'),
            'total_credits_reset': sum(t.value for t in transactions 
                if t.primary_type == 'user_story' and t.transaction_type == 'reset'),
            'total_scans_received': sum(t.value for t in transactions 
                if t.primary_type == 'test_case' and t.transaction_type == 'received'),
            'total_scans_used': sum(t.value for t in transactions 
                if t.primary_type == 'test_case' and t.transaction_type == 'used'),
            'total_scans_reset': sum(t.value for t in transactions 
                if t.primary_type == 'test_case' and t.transaction_type == 'reset')
        }
        
        return jsonify({
            'user': user_data,
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


@app.route('/api/process-expired-users', methods=['POST'])
def process_expired_users():
    db = SessionLocal()
    try:
        current_time = datetime.utcnow()
        processed_users = {
            'newly_blocked': 0,
            'credits_removed': 0,
            'soft_deleted': 0
        }
        
        # validity_expiration -> 90 days
        # is_blocked
        # credit_cleanup_date -> 30days more
        # is_deleted -> 150 days more

        # 1. Find users with expired validity but not yet blocked (handling 90days)
        expired_users = db.query(User).filter(
            User.validity_expiration < current_time,
            User.credit_cleanup_date.is_(None),
            User.is_deleted.is_(False),
            User.is_blocked.is_(False)
        ).all()

        for user in expired_users:
            user.is_blocked = True
            user.credit_cleanup_date = current_time + timedelta(days=30)
            user.account_deletion_date = current_time + timedelta(days=180)  # 180 days
            processed_users['newly_blocked'] += 1

        # 2. Find users ready for credit removal (30 days after blocking)
        credit_removal_users = db.query(User).filter(
            User.credit_cleanup_date < current_time,
            or_(
                User.current_test_case > 0,
                User.current_user_story > 0
            ),
            User.is_deleted.is_(False)
        ).all()

        for user in credit_removal_users:
            # Remove test case credits
            if user.current_test_case > 0:
                db.add(Transaction(
                    user_id=user.id,
                    primary_type='test_case',
                    source_type='system',
                    transaction_type='reset',
                    value=-user.current_test_case,
                    description='Credits reset after 30 days of account blocking'
                ))
                user.current_test_case = 0

            # Remove user story credits
            if user.current_user_story > 0:
                db.add(Transaction(
                    user_id=user.id,
                    primary_type='user_story',
                    source_type='system',
                    transaction_type='reset',
                    value=-user.current_user_story,
                    description='Credits reset after 30 days of account blocking'
                ))
                user.current_user_story = 0

            processed_users['credits_removed'] += 1

        # 3. Find users ready for deletion (180 days after initial blocking)
        deletion_users = db.query(User).filter(
            User.account_deletion_date < current_time,
            User.is_deleted.is_(False)
        ).all()

        for user in deletion_users:
            user.is_deleted = True
            processed_users['soft_deleted'] += 1

        db.commit()

        return jsonify({
            'status': 'success',
            'processed': processed_users,
            'timestamp': current_time.isoformat()
        }), 200

    except Exception as e:
        db.rollback()
        print(f"Error processing expired users: {str(e)}")
        return jsonify({'error': str(e)}), 500
    finally:
        db.close()

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




        # elif event['type'] == 'invoice.paid':
        #     invoice = event['data']['object']
        #     subscription_id = invoice.get('subscription')
            
        #     if subscription_id:
        #         subscription = stripe.Subscription.retrieve(subscription_id)
        #         product = stripe.Product.retrieve(subscription.plan.product)
        #         customer = stripe.Customer.retrieve(invoice.customer)
        #         user = get_user_by_email(customer.email, db)
                
        #         # Get subscription details
        #         is_yearly = subscription.plan.interval == 'year'
        #         credits_key = 'base_credits_yearly' if is_yearly else 'base_credits_monthly'
        #         scans_key = 'base_scans_yearly' if is_yearly else 'base_scans_monthly'
                
        #         credits = int(product.metadata.get(credits_key, 0))
        #         scans = int(product.metadata.get(scans_key, 0))

        #         # Check if this is a subscription renewal
        #         is_renewal = invoice.get('billing_reason') == 'subscription_cycle'
                
        #         if is_renewal and user.stripe_subscription_id == subscription_id:
        #             print(f"Processing subscription renewal for user {user.email}")
        #             print(f"Previous credits: {user.current_credits}, Previous scans: {user.current_scans}")
                    
        #             # Create transactions for renewal credits and scans
        #             credit_transaction = Transaction(
        #                 user_id=user.id,
        #                 primary_type='credit',
        #                 source_type='subscription',
        #                 transaction_type='received',
        #                 value=credits,
        #                 subscription_id=subscription_id,
        #                 description=f'Renewal credits for {product.name} subscription'
        #             )
        #             db.add(credit_transaction)
                    
        #             scan_transaction = Transaction(
        #                 user_id=user.id,
        #                 primary_type='scan',
        #                 source_type='subscription',
        #                 transaction_type='received',
        #                 value=scans,
        #                 subscription_id=subscription_id,
        #                 description=f'Renewal scans for {product.name} subscription'
        #             )
        #             db.add(scan_transaction)
                    
        #             # Add new values to existing ones
        #             user.current_credits += credits
        #             user.current_scans += scans
                    
        #             print(f"New totals - Credits: {user.current_credits}, Scans: {user.current_scans}")

        #         elif user.is_blocked:
        #             # For blocked users, add new credits/scans to existing ones
        #             print(f"Resubscription for blocked user {user.email}")
        #             print(f"Previous credits: {user.current_credits}, Previous scans: {user.current_scans}")
                    
        #             # Create transactions for additional credits and scans
        #             credit_transaction = Transaction(
        #                 user_id=user.id,
        #                 primary_type='credit',
        #                 source_type='subscription',
        #                 transaction_type='received',
        #                 value=credits,
        #                 subscription_id=subscription_id,
        #                 description=f'Additional credits from resubscription to {product.name}'
        #             )
        #             db.add(credit_transaction)
                    
        #             scan_transaction = Transaction(
        #                 user_id=user.id,
        #                 primary_type='scan',
        #                 source_type='subscription',
        #                 transaction_type='received',
        #                 value=scans,
        #                 subscription_id=subscription_id,
        #                 description=f'Additional scans from resubscription to {product.name}'
        #             )
        #             db.add(scan_transaction)
                    
        #             # Add new values to existing ones
        #             user.current_credits += credits
        #             user.current_scans += scans
        #             user.max_users = int(product.metadata.get('users', 0))
                    
        #             # Reset blocked status
        #             user.is_blocked = False
        #             user.benefits_end_date = None
        #             user.stripe_subscription_id = subscription_id
                    
        #             print(f"New totals - Credits: {user.current_credits}, Scans: {user.current_scans}")

        #         elif not user.stripe_subscription_id or invoice.get('billing_reason') == 'subscription_create':
        #             # Brand new subscription
        #             print(f"New subscription for user {user.email}")
                    
        #             # If transitioning from trial, create reset transactions
        #             if user.has_used_trial:
        #                 credit_reset_transaction = Transaction(
        #                     user_id=user.id,
        #                     primary_type='credit',
        #                     source_type='subscription',
        #                     transaction_type='reset',
        #                     value=user.current_credits,
        #                     subscription_id=subscription_id,
        #                     description='Reset credits from trial to subscription'
        #                 )
        #                 db.add(credit_reset_transaction)

        #                 scan_reset_transaction = Transaction(
        #                     user_id=user.id,
        #                     primary_type='scan',
        #                     source_type='subscription',
        #                     transaction_type='reset',
        #                     value=user.current_scans,
        #                     subscription_id=subscription_id,
        #                     description='Reset scans from trial to subscription'
        #                 )
        #                 db.add(scan_reset_transaction)
                    
        #             # Create transactions for initial credits and scans
        #             credit_transaction = Transaction(
        #                 user_id=user.id,
        #                 primary_type='credit',
        #                 source_type='subscription',
        #                 transaction_type='received',
        #                 value=credits,
        #                 subscription_id=subscription_id,
        #                 description=f'Initial credits for {product.name} subscription'
        #             )
        #             db.add(credit_transaction)
                    
        #             scan_transaction = Transaction(
        #                 user_id=user.id,
        #                 primary_type='scan',
        #                 source_type='subscription',
        #                 transaction_type='received',
        #                 value=scans,
        #                 subscription_id=subscription_id,
        #                 description=f'Initial scans for {product.name} subscription'
        #             )
        #             db.add(scan_transaction)
                    
        #             # Set initial values
        #             user.current_credits = credits
        #             user.current_scans = scans
        #             user.max_users = int(product.metadata.get('users', 0))
        #             user.stripe_subscription_id = subscription_id
        #             user.has_used_trial = True
        #             user.trial_end_date = None
                
        #         db.commit()
        #         print(f"Successfully processed subscription update for user {user.email}")

        # # catch the subscription updated .
        # elif event['type'] == 'customer.subscription.updated':
        #     subscription = event['data']['object']
        #     previous_attributes = event['data']['previous_attributes']
            
        #     # Only process price updates
        #     if 'items' in previous_attributes and 'data' in previous_attributes['items']:
        #         # Get customer and user
        #         customer_id = subscription['customer']
        #         customer = stripe.Customer.retrieve(customer_id)
        #         user = get_user_by_email(customer.email, db)
                
        #         if not user:
        #             raise Exception(f"User not found for customer {customer_id}")
                
        #         # Get the new product details
        #         new_price_id = subscription['items']['data'][0]['price']['id']
        #         new_price = stripe.Price.retrieve(new_price_id)
        #         new_product = stripe.Product.retrieve(new_price['product'])
                
        #         # Get the old product details
        #         old_price_id = previous_attributes['items']['data'][0]['price']['id']
        #         old_price = stripe.Price.retrieve(old_price_id)
        #         old_product = stripe.Product.retrieve(old_price['product'])
                
        #         # Determine if it's monthly or yearly subscription
        #         is_yearly = subscription['items']['data'][0]['plan']['interval'] == 'year'
        #         credits_key = 'base_credits_yearly' if is_yearly else 'base_credits_monthly'
        #         scans_key = 'base_scans_yearly' if is_yearly else 'base_scans_monthly'
                
        #         # Get new limits from product metadata
        #         new_credits = int(new_product.metadata.get(credits_key, 0))
        #         new_scans = int(new_product.metadata.get(scans_key, 0))
        #         new_max_users = int(new_product.metadata.get('users', 0))
                
        #         # Create transactions for additional credits and scans
        #         credit_transaction = Transaction(
        #             user_id=user.id,
        #             primary_type='credit',
        #             source_type='subscription',
        #             transaction_type='received',
        #             value=new_credits,
        #             subscription_id=subscription.id,
        #             description=f'Additional credits from upgrade: {old_product.name} to {new_product.name}'
        #         )
        #         db.add(credit_transaction)
                
        #         scan_transaction = Transaction(
        #             user_id=user.id,
        #             primary_type='scan',
        #             source_type='subscription',
        #             transaction_type='received',
        #             value=new_scans,
        #             subscription_id=subscription.id,
        #             description=f'Additional scans from upgrade: {old_product.name} to {new_product.name}'
        #         )
        #         db.add(scan_transaction)
                
        #         # Add new values to existing credits and scans
        #         user.current_credits += new_credits
        #         user.current_scans += new_scans
        #         user.max_users = new_max_users 
                
        #         db.commit()
        #         print(f"Successfully processed subscription upgrade for user {user.email}")
        #         print(f"Previous credits: {user.current_credits - new_credits}, Previous scans: {user.current_scans - new_scans}")
        #         print(f"Added credits: {new_credits}, Added scans: {new_scans}")
        #         print(f"New totals - Credits: {user.current_credits}, Scans: {user.current_scans}, Max Users: {new_max_users}")

        # # catch subscription deleted.
        # elif event['type'] == 'customer.subscription.deleted':
        #     subscription = event['data']['object']
            
        #     # Get customer and user
        #     customer_id = subscription['customer']
        #     customer = stripe.Customer.retrieve(customer_id)
        #     user = get_user_by_email(customer.email, db)
            
        #     if not user:
        #         raise Exception(f"User not found for customer {customer_id}")
            
        #     # Set blocked status and benefits end date (3 months from now)
        #     user.is_blocked = True
        #     user.benefits_end_date = datetime.utcnow() + timedelta(days=90)  # 3 months
            
        #     print(f"Subscription cancelled for user {user.email}. Benefits will expire on {user.benefits_end_date}")
        #     db.commit()    
        