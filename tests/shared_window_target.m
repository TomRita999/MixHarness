#import <AppKit/AppKit.h>
#import <math.h>
#import <unistd.h>

@interface ButtonCounter : NSObject
@property(nonatomic) NSInteger count;
- (void)pressed:(id)sender;
@end

@implementation ButtonCounter
- (void)pressed:(id)sender { self.count += 1; }
@end

static NSWindow *windowAt(NSRect frame, NSString *title) {
    NSWindow *window = [[NSWindow alloc] initWithContentRect:frame styleMask:NSWindowStyleMaskTitled
                                                    backing:NSBackingStoreBuffered defer:NO];
    window.title = title;
    window.restorable = NO;
    return window;
}

static NSArray *centerInCapture(NSView *view) {
    NSPoint center = NSMakePoint(NSMidX(view.bounds), NSMidY(view.bounds));
    NSPoint point = [view convertPoint:center toView:nil];
    NSRect frame = view.window.frame;
    return @[@(point.x / NSWidth(frame)), @(1.0 - point.y / NSHeight(frame))];
}

int main(void) {
    @autoreleasepool {
        NSArray<NSString *> *args = NSProcessInfo.processInfo.arguments;
        if (args.count < 3) return 2;
        NSString *directory = args[args.count - 2];
        NSString *document = args.lastObject;
        NSError *error = nil;
        NSString *contents = [NSString stringWithContentsOfFile:document encoding:NSUTF8StringEncoding error:&error];
        if (!contents) { NSLog(@"%@", error); return 1; }
        NSApplication *app = NSApplication.sharedApplication;
        [app setActivationPolicy:NSApplicationActivationPolicyRegular];
        [app finishLaunching];
        if (!NSScreen.mainScreen) return 1;
        NSRect visible = NSScreen.mainScreen.visibleFrame;
        CGFloat x = NSMinX(visible) + 12, y = NSMinY(visible) + 12;
        NSWindow *window = windowAt(NSMakeRect(x, y, 360, 240), directory.lastPathComponent);
        NSWindow *sibling = windowAt(NSMakeRect(x + 190, y + 140, 175, 100), @"另一测试窗口");
        sibling.backgroundColor = NSColor.redColor;
        NSTextField *otherField = [NSTextField textFieldWithString:document.lastPathComponent];
        otherField.frame = NSMakeRect(8, 8, 155, 24);
        [sibling.contentView addSubview:otherField];

        ButtonCounter *counter = [ButtonCounter new];
        NSButton *button = [NSButton buttonWithTitle:@"记录点击" target:counter action:@selector(pressed:)];
        button.frame = NSMakeRect(12, 188, 140, 34);
        [window.contentView addSubview:button];
        NSBox *swatch = [[NSBox alloc] initWithFrame:NSMakeRect(215, 185, 110, 40)];
        swatch.boxType = NSBoxCustom;
        swatch.borderWidth = 0;
        swatch.transparent = NO;
        swatch.fillColor = [NSColor colorWithSRGBRed:0.12 green:0.75 blue:0.20 alpha:1.0];
        [window.contentView addSubview:swatch];
        NSScrollView *scroll = [NSTextView scrollableTextView];
        scroll.frame = NSMakeRect(12, 12, 336, 158);
        scroll.scrollerStyle = NSScrollerStyleLegacy;
        scroll.hasVerticalScroller = YES;
        scroll.autohidesScrollers = NO;
        [window.contentView addSubview:scroll];
        NSTextView *editor = (NSTextView *)scroll.documentView;
        editor.richText = NO;
        editor.string = contents;
        [editor setSelectedRange:NSMakeRange(0, 0)];
        [editor scrollRangeToVisible:NSMakeRange(0, 0)];
        [sibling orderBack:nil];
        [window makeKeyAndOrderFront:nil];
        [window makeFirstResponder:editor];
        NSDate *launch = NSRunningApplication.currentApplication.launchDate;
        if (!launch) return 1;
        NSString *target = [NSString stringWithFormat:@"%d-%lld-%u", getpid(),
                            llround(launch.timeIntervalSince1970 * 1000000), (unsigned)window.windowNumber];
        NSString *commandPath = [directory stringByAppendingPathComponent:@"command.json"];
        NSString *replyPath = [directory stringByAppendingPathComponent:@"reply.json"];
        __block NSInteger last = -1;
        NSTimeInterval deadline = NSProcessInfo.processInfo.systemUptime + 150;
        [NSTimer scheduledTimerWithTimeInterval:0.04 repeats:YES block:^(NSTimer *timer) {
            if (NSProcessInfo.processInfo.systemUptime > deadline ||
                ![NSFileManager.defaultManager fileExistsAtPath:directory]) { [app terminate:nil]; return; }
            NSData *input = [NSData dataWithContentsOfFile:commandPath];
            if (!input) return;
            NSError *failure = nil;
            NSDictionary *command = [NSJSONSerialization JSONObjectWithData:input options:0 error:&failure];
            if (!command) { NSLog(@"%@", failure); exit(1); }
            NSInteger sequence = [command[@"seq"] integerValue];
            if (sequence == last) return;
            last = sequence;
            NSString *action = command[@"action"];
            if ([action isEqual:@"quit"]) { [app terminate:nil]; return; }
            if ([action isEqual:@"sibling"]) {
                [sibling makeKeyAndOrderFront:nil];
                [sibling makeFirstResponder:otherField];
            } else if ([action isEqual:@"editor"]) {
                [window makeKeyAndOrderFront:nil];
                [window makeFirstResponder:editor];
            } else if (![action isEqual:@"inspect"]) { exit(2); }
            NSDictionary *state = @{@"seq":@(sequence), @"target":target, @"title":window.title,
                @"width":@(NSWidth(window.frame)), @"height":@(NSHeight(window.frame)),
                @"button":centerInCapture(button), @"swatch":centerInCapture(swatch),
                @"scrollArea":centerInCapture(scroll.contentView), @"scrollY":@(NSMinY(scroll.contentView.bounds)),
                @"clicks":@(counter.count), @"text":editor.string, @"length":@(editor.string.length),
                @"selected":@(editor.selectedRange.length), @"otherText":otherField.stringValue};
            NSData *output = [NSJSONSerialization dataWithJSONObject:state options:0 error:&failure];
            if (!output || ![output writeToFile:replyPath options:NSDataWritingAtomic error:&failure]) {
                NSLog(@"%@", failure); exit(1);
            }
        }];
        [app run];
    }
    return 0;
}
